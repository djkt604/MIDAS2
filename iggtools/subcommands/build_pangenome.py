#!/usr/bin/env python3
import os
import sys
from collections import defaultdict
from multiprocessing import Semaphore
import Bio.SeqIO
from iggtools.common.argparser import add_subcommand, SUPPRESS
from iggtools.common.utils import tsprint, InputStream, OutputStream, retry, command, split, multiprocessing_map, multithreading_hashmap, multithreading_map, num_vcpu, select_from_tsv, transpose, find_files, upload, upload_star, flatten, pythonpath
from iggtools.models.uhgg import UHGG, get_uhgg_layout, destpath
from iggtools.params import outputs


CLUSTERING_PERCENTS = [99, 95, 90, 85, 80, 75]
CLUSTERING_PERCENTS = sorted(CLUSTERING_PERCENTS, reverse=True)


# Up to this many concurrent species builds.
CONCURRENT_SPECIES_BUILDS = Semaphore(3)


def pan_destpath(species_id, filename):
    return destpath(get_uhgg_layout(species_id, filename)["pangenome_file"])


@retry
def find_files_with_retry(f):
    return find_files(f)


def decode_species_arg(args, species):
    selected_species = set()
    try:  # pylint: disable=too-many-nested-blocks
        if args.species.upper() == "ALL":
            selected_species = set(species)
        else:
            for s in args.species.split(","):
                if ":" not in s:
                    assert str(int(s)) == s, f"Species id is not an integer: {s}"
                    selected_species.add(s)
                else:
                    i, n = s.split(":")
                    i = int(i)
                    n = int(n)
                    assert 0 <= i < n, f"Species class and modulus make no sense: {i}, {n}"
                    for sid in species:
                        if int(sid) % n == i:
                            selected_species.add(sid)
    except:
        tsprint(f"ERROR:  Species argument is not a list of species ids or slices: {s}")
        raise
    return sorted(selected_species)


# 1. Occasional failures in aws s3 cp require a retry.
# 2. In future, for really large numbers of genomes, we may prefer a separate wave of retries for all first-attempt failures.
# 3. The Bio.SeqIO.parse() code is CPU-bound and thus it's best to run this function in a separate process for every genome.
@retry
def clean_genes(packed_ids):
    species_id, genome_id = packed_ids
    #input_annotations = destpath(get_uhgg_layout(species_id, "ffn", genome_id)["annotation_file"])
    input_annotations = midas_db.get_target_layout("annotation_file", True, species_id, genome_id, "ffn")
    #TODO: double check the above
    
    output_genes = f"{genome_id}.genes.ffn"
    output_info = f"{genome_id}.genes.len"

    with open(output_genes, 'w') as o_genes, \
         open(output_info, 'w') as o_info, \
         InputStream(input_annotations, check_path=False) as genes:  # check_path=False because for flat directory structure it's slow
        for rec in Bio.SeqIO.parse(genes, 'fasta'):
            gene_id = rec.id
            gene_seq = str(rec.seq).upper()
            gene_len = len(gene_seq)
            if gene_len == 0 or gene_id == '' or gene_id == '|':
                # Documentation for why we ignore these gene_ids should be added to
                # https://github.com/czbiohub/iggtools/wiki#pan-genomes
                # Also, we should probably count these and report stats.
                pass
            else:
                o_genes.write(f">{gene_id}\n{gene_seq}\n")
                o_info.write(f"{gene_id}\t{genome_id}\t{gene_len}\n")

    return output_genes, output_info


def vsearch(percent_id, genes, num_threads=num_vcpu):
    centroids = f"centroids.{percent_id}.ffn"
    uclust = f"uclust.{percent_id}.txt"
    # log = f"uclust.{percent_id}.log"
    if find_files(centroids) and find_files(uclust):
        tsprint(f"Found vsearch results at percent identity {percent_id} from prior run.")
    else:
        try:
            command(f"vsearch --quiet --cluster_fast {genes} --id {percent_id/100.0} --threads {num_threads} --centroids {centroids} --uc {uclust}")
        except:
            # Do not keep bogus zero-length files;  those are harmful if we rerun in place.
            command(f"mv {centroids} {centroids}.bogus", check=False)
            command(f"mv {uclust} {uclust}.bogus", check=False)
            raise
    return centroids, uclust #, log


def parse_uclust(uclust_file, select_columns):
    # The uclust TSV file does not contain a header line.  So, we have to hardcode the schema here.  Then select specified columns.
    all_uclust_columns = ['type', 'cluster_id', 'size', 'pid', 'strand', 'skip1', 'skip2', 'skip3', 'gene_id', 'centroid_id']
    with InputStream(uclust_file) as ucf:
        for r in select_from_tsv(ucf, select_columns, all_uclust_columns):
            yield r


def xref(cluster_files, gene_info_file):
    """
    Produce the gene_info.txt file as documented in https://github.com/czbiohub/iggtools/wiki#pan-genomes
    """
    # Let centroid_info[gene][percent_id] be the centroid of the percent_id cluster contianing gene.
    # The max_percent_id centroids are computed directly for all genes.  Only these centroids are
    # then reclustered to lower percent_id's.
    #
    # The centroids are themselves genes, and their ids, as all gene_ids, are strings
    # generated by the annotation tool prodigal.
    centroid_info = defaultdict(dict)
    for percent_id, (_, uclust_file) in cluster_files.items():
        for r_type, r_gene, r_centroid in parse_uclust(uclust_file, ['type', 'gene_id', 'centroid_id']):
            if r_type == 'S':
                # r itself is the centroid of its cluster
                centroid_info[r_gene][percent_id] = r_gene
            elif r_type == 'H':
                # r is not itself a centroid
                centroid_info[r_gene][percent_id] = r_centroid
            else:
                # ignore all other r types
                pass

    # Check for a problem that occurs with improper import of genomes (when contig names clash).
    percents = cluster_files.keys()
    max_percent_id = max(percents)
    for g in centroid_info:
        cg = centroid_info[g][max_percent_id]
        ccg = centroid_info[cg][max_percent_id]
        assert cg == ccg, f"The {max_percent_id}-centroid relation should be idempotent, however, {cg} != {ccg}.  See https://github.com/czbiohub/iggtools/issues/16"

    # At this point we have the max_percent_id centroid for any gene gc, but we lack
    # coarser clustering assignments for many genes -- we only have those for genes
    # that are themelves centroids of max_percent_id clusters.
    #
    # We can infer the remaining cluster assignments for all genes by transitivity.
    # For any gene gc, look up the clusters containing gc's innermost centroid,
    # gc[max_percent_id].  Those clusters also contain gc.
    for gc in centroid_info.values():
        gc_recluster = centroid_info[gc[max_percent_id]]
        for percent_id in percents:
            gc[percent_id] = gc_recluster[percent_id]

    with OutputStream(gene_info_file) as gene_info:
        header = ['gene_id'] + [f"centroid_{pid}" for pid in percents]
        gene_info.write('\t'.join(header) + '\n')
        genes = centroid_info.keys()
        for gene_id in sorted(genes):
            gene_info.write(gene_id)
            for centroid in centroid_info[gene_id].values():
                gene_info.write('\t')
                gene_info.write(centroid)
            gene_info.write('\n')


def build_pangenome(args):
    if args.zzz_worker_toc:
        build_pangenome_worker(args)
    else:
        build_pangenome_master(args)


def build_pangenome_master(args):

    # Fetch table of contents from s3.
    # This will be read separately by each species build subcommand, so we make a local copy.
    local_toc = os.path.basename(outputs.genomes)
    command(f"rm -f {local_toc}")
    command(f"aws s3 cp --only-show-errors {outputs.genomes} {local_toc}")

    db = UHGG(local_toc)
    species = db.species

    def species_work(species_id):
        assert species_id in species, f"Species {species_id} is not in the database."
        species_genomes = species[species_id]

        # The species build will upload this file last, after everything else is successfully uploaded.
        # Therefore, if this file exists in s3, there is no need to redo the species build.
        dest_file = pan_destpath(species_id, "gene_info.txt")
        msg = f"Building pangenome for species {species_id} with {len(species_genomes)} total genomes."
        if find_files_with_retry(dest_file):
            if not args.force:
                tsprint(f"Destination {dest_file} for species {species_id} pangenome already exists.  Specify --force to overwrite.")
                return
            msg = msg.replace("Building", "Rebuilding")

        with CONCURRENT_SPECIES_BUILDS:
            tsprint(msg)
            logfile = get_uhgg_layout(species_id)["pangenome_log"]
            worker_log = os.path.basename(logfile)
            worker_subdir = str(species_id)
            if not args.debug:
                command(f"rm -rf {worker_subdir}")
            if not os.path.isdir(worker_subdir):
                command(f"mkdir {worker_subdir}")
            # Recurisve call via subcommand.  Use subdir, redirect logs.
            worker_cmd = f"cd {worker_subdir}; PYTHONPATH={pythonpath()} {sys.executable} -m iggtools build_pangenome -s {species_id} --zzz_worker_mode --zzz_worker_toc {os.path.abspath(local_toc)} {'--debug' if args.debug else ''} &>> {worker_log}"
            with open(f"{worker_subdir}/{worker_log}", "w") as slog:
                slog.write(msg + "\n")
                slog.write(worker_cmd + "\n")
            try:
                command(worker_cmd)
            finally:
                # Cleanup should not raise exceptions of its own, so as not to interfere with any
                # prior exceptions that may be more informative.  Hence check=False.
                upload(f"{worker_subdir}/{worker_log}", destpath(logfile), check=False)
                if not args.debug:
                    command(f"rm -rf {worker_subdir}", check=False)

    # Check for destination presence in s3 with up to 10-way concurrency.
    # If destination is absent, commence build with up to 3-way concurrency as constrained by CONCURRENT_SPECIES_BUILDS.
    species_id_list = decode_species_arg(args, species)
    multithreading_map(species_work, species_id_list, num_threads=10)


def build_pangenome_worker(args):
    """
    Input spec:  https://github.com/czbiohub/iggtools/wiki#gene-annotations
    Output spec: https://github.com/czbiohub/iggtools/wiki#pan-genomes
    """

    violation = "Please do not call build_pangenome_worker directly.  Violation"
    assert args.zzz_worker_mode, f"{violation}:  Missing --zzz_worker_mode arg."
    assert os.path.isfile(args.zzz_worker_toc), f"{violation}: File does not exist: {args.zzz_worker_toc}"
    assert os.path.basename(os.getcwd()) == args.species, f"{violation}: {os.path.basename(os.getcwd())} != {args.species}"

    db = UHGG(args.zzz_worker_toc)
    species = db.species
    species_id = args.species

    assert species_id in species, f"{violation}: Species {species_id} is not in the database."

    species_genomes = species[species_id]
    species_genomes_ids = species_genomes.keys()

    cleaned = multiprocessing_map(clean_genes, ((species_id, genome_id) for genome_id in species_genomes_ids))

    command("rm -f genes.ffn genes.len")
    for temp_files in split(cleaned, 20):  # keep "cat" commands short
        ffn_files, len_files = transpose(temp_files)
        command("cat " + " ".join(ffn_files) + " >> genes.ffn")
        command("cat " + " ".join(len_files) + " >> genes.len")

    # The initial clustering to max_percent takes longest.
    max_percent, lower_percents = CLUSTERING_PERCENTS[0], CLUSTERING_PERCENTS[1:]
    cluster_files = {max_percent: vsearch(max_percent, "genes.ffn")}

    # Reclustering of the max_percent centroids is usually quick, and can proceed in prallel.
    recluster = lambda percent_id: vsearch(percent_id, cluster_files[max_percent][0])
    cluster_files.update(multithreading_hashmap(recluster, lower_percents))

    xref(cluster_files, "gene_info.txt")

    # Create list of (source, dest) pairs for uploading.
    # Note that centroids.{max_percent}.ffn is uploaded to two different destinations.
    upload_tasks = [
        ("genes.ffn", pan_destpath(species_id, "genes.ffn")),
        ("genes.len", pan_destpath(species_id, "genes.len")),
        (f"centroids.{max_percent}.ffn", pan_destpath(species_id, "centroids.ffn"))
    ]

    for src in flatten(cluster_files.values()):
        upload_tasks.append((src, pan_destpath(species_id, f"temp/{src}")))

    # Upload in parallel.
    last_output = "gene_info.txt"
    last_dest_file = pan_destpath(species_id, last_output)
    command(f"aws s3 rm --recursive {os.path.dirname(last_dest_file)}")
    multithreading_map(upload_star, upload_tasks)

    # Leave this upload for last, so the presence of this file in s3 would indicate the entire species build has succeeded.
    upload(last_output, last_dest_file)


def register_args(main_func):
    subparser = add_subcommand('build_pangenome', main_func, help='build pangenome for specified species')
    subparser.add_argument('-s',
                           '--species',
                           dest='species',
                           required=False,
                           help="species[,species...] whose pangenome(s) to build;  alternatively, species slice in format idx:modulus, e.g. 1:30, meaning build species whose ids are 1 mod 30; or, the special keyword 'all' meaning all species")
    subparser.add_argument('--zzz_worker_toc',
                           dest='zzz_worker_toc',
                           required=False,
                           help=SUPPRESS) # "reserved to pass table of contents from master to worker"
    return main_func


@register_args
def main(args):
    tsprint(f"Executing iggtools subcommand {args.subcommand} with args {vars(args)}.")
    build_pangenome(args)
