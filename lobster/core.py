import daemon
import logging
import multiprocessing
import os
import datetime
import signal
import sys
import time
import traceback
import yaml
import re

from functools import partial
from lobster import util, cmssw, job

from pkg_resources import get_distribution

import work_queue as wq

logger = multiprocessing.get_logger()

def kill(args):
    logger.info("setting flag to quit at the next checkpoint")
    with open(args.configfile) as configfile:
        config = yaml.load(configfile)

    workdir = config['workdir']
    util.register_checkpoint(workdir, 'KILLED', 'PENDING')

def run(args):
    with open(args.configfile) as configfile:
        config = yaml.load(configfile)

    workdir = config['workdir']
    if not os.path.exists(workdir):
        os.makedirs(workdir)
        util.register_checkpoint(workdir, "version", get_distribution('Lobster').version)
    else:
        util.verify(workdir)

    cmsjob = False
    if config.get('type', 'cmssw') == 'cmssw':
        cmsjob = True

        from ProdCommon.Credential.CredentialAPI import CredentialAPI
        cred = CredentialAPI({'credential': 'Proxy'})
        if cred.checkCredential(Time=60):
            if not 'X509_USER_PROXY' in os.environ:
                os.environ['X509_USER_PROXY'] = cred.credObj.getUserProxy()
        else:
            if config.get('check proxy', True):
                try:
                    cred.ManualRenewCredential()
                except Exception as e:
                    logger.critical("could not renew proxy")
                    sys.exit(1)
            else:
                logger.critical("please renew your proxy")
                sys.exit(1)

    mode = 'merge' if args.merge else 'process'
    print "Saving log to {0}".format(os.path.join(workdir, mode + '.log'))

    if not args.foreground:
        ttyfile = open(os.path.join(workdir, mode + '.err'), 'a')
        print "Saving stderr and stdout to {0}".format(os.path.join(workdir, mode + '.err'))

    signals = daemon.daemon.make_default_signal_map()
    signals[signal.SIGTERM] = lambda num, frame: kill(args)

    with daemon.DaemonContext(
            detach_process=not args.foreground,
            stdout=sys.stdout if args.foreground else ttyfile,
            stderr=sys.stderr if args.foreground else ttyfile,
            working_directory=workdir,
            pidfile=util.get_lock(workdir),
            signal_map=signals):

        fileh = logging.FileHandler(os.path.join(workdir, mode + '.log'))
        fileh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] - %(pathname)s %(lineno)d: %(message)s"))
        fileh.setLevel(config.get('log level', 2) * 10)

        logger.addHandler(fileh)
        logger.setLevel(config.get('log level', 2) * 10)

        if args.foreground:
            console = logging.StreamHandler()
            console.setLevel(config.get('log level', 2) * 10)
            console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] - %(pathname)s %(lineno)d: %(message)s"))
            logger.addHandler(console)

        config['configdir'] = args.configdir
        config['filename'] = args.configfile
        config['startdir'] = args.startdir
        if args.merge:
            if args.server:
                config['stageout server'] = args.server
            config['max megabytes'] = args.max_megabytes
            config['datasets to merge'] = args.datasets
            job_src = cmssw.MergeProvider(config)
            actions = cmssw.Actions(config)
        elif cmsjob:
            job_src = cmssw.JobProvider(config)
            actions = cmssw.Actions(config)
        else:
            job_src = job.SimpleJobProvider(config)
            actions = None

        logger.info("using wq from {0}".format(wq.__file__))

        wq.cctools_debug_flags_set("all")
        wq.cctools_debug_config_file(os.path.join(workdir, mode + "_work_queue_debug.log"))
        wq.cctools_debug_config_file_size(1 << 29)

        queue = wq.WorkQueue(-1)
        queue.specify_log(os.path.join(workdir, mode + "_work_queue.log"))
        queue.specify_name("lobster_" + mode + "_" + config["id"])
        queue.specify_keepalive_timeout(300)
        # queue.tune("short-timeout", 600)
        queue.tune("transfer-outlier-factor", 4)
        queue.specify_algorithm(wq.WORK_QUEUE_SCHEDULE_RAND)

        logger.info("starting queue as {0}".format(queue.name))
        logger.info("submit workers with: condor_submit_workers -M {0} <num>".format(queue.name))

        payload = config.get('tune', {}).get('payload', 400)
        abort_active = False
        abort_threshold = config.get('tune', {}).get('abort threshold', 400)
        abort_multiplier = config.get('tune', {}).get('abort multiplier', 4)

        if util.checkpoint(workdir, 'KILLED') == 'PENDING':
            util.register_checkpoint(workdir, 'KILLED', 'RESTART')

        jobits_left = 0
        successful_jobs = 0

        creation_time = 0
        destruction_time = 0

        with open(os.path.join(workdir, mode + "_stats.log"), "a") as statsfile:
            statsfile.write(
                    "#timestamp " +
                    "total_workers_connected total_workers_joined total_workers_removed " +
                    "workers_busy workers_idle " +
                    "tasks_running " +
                    "total_send_time total_receive_time " +
                    "total_create_time total_return_time " +
                    "idle_percentage " +
                    "capacity " +
                    "efficiency " +
                    "jobits_left\n")

        while not job_src.done():
            jobits_left = job_src.work_left()
            stats = queue.stats

            with open(os.path.join(workdir, mode + "_stats.log"), "a") as statsfile:
                now = datetime.datetime.now()
                statsfile.write(" ".join(map(str,
                    [
                        int(int(now.strftime('%s')) * 1e6 + now.microsecond),
                        stats.total_workers_connected,
                        stats.total_workers_joined,
                        stats.total_workers_removed,
                        stats.workers_busy,
                        stats.workers_idle,
                        stats.tasks_running,
                        stats.total_send_time,
                        stats.total_receive_time,
                        creation_time,
                        destruction_time,
                        stats.idle_percentage,
                        stats.capacity,
                        stats.efficiency,
                        jobits_left
                    ]
                    )) + "\n"
                )

            if util.checkpoint(workdir, 'KILLED') == 'PENDING':
                util.register_checkpoint(workdir, 'KILLED', str(datetime.datetime.utcnow()))
                logger.info("terminating gracefully")
                break

            logger.info("{0} out of {1} workers busy; {3} jobs running, {4} waiting; {2} jobits left".format(
                    stats.workers_busy,
                    stats.workers_busy + stats.workers_ready,
                    jobits_left,
                    stats.tasks_running,
                    stats.tasks_waiting))

            hunger = max(payload - stats.tasks_waiting, 0)

            t = time.time()
            while hunger > 0:
                jobs = job_src.obtain(50)

                if jobs == None or len(jobs) == 0:
                    break

                hunger -= len(jobs)

                for id, cmd, inputs, outputs in jobs:
                    task = wq.Task(cmd)
                    task.specify_tag(id)
                    task.specify_cores(1)
                    # temporary work-around?
                    # task.specify_memory(1000)
                    # task.specify_disk(4000)

                    for (local, remote) in inputs:
                        if os.path.isfile(local):
                            cache_opt = wq.WORK_QUEUE_NOCACHE if str(remote) == 'proxy' else wq.WORK_QUEUE_CACHE
                            task.specify_input_file(str(local), str(remote), cache_opt)
                        elif os.path.isdir(local):
                            task.specify_directory(local, remote, wq.WORK_QUEUE_INPUT,
                                    wq.WORK_QUEUE_CACHE, recursive=True)
                        else:
                            logger.critical("cannot send file to worker: {0}".format(local))
                            raise NotImplementedError

                    for (local, remote) in outputs:
                        task.specify_output_file(str(local), str(remote))

                    queue.submit(task)
            creation_time += int((time.time() - t) * 1e6)

            task = queue.wait(300)
            tasks = []
            while task:
                if task.return_status == 0:
                    successful_jobs += 1
                tasks.append(task)
                if queue.stats.tasks_complete > 0:
                    task = queue.wait(1)
                else:
                    task = None
            if len(tasks) > 0:
                try:
                    t = time.time()
                    job_src.release(tasks)
                    destruction_time += int((time.time() - t) * 1e6)
                except:
                    tb = traceback.format_exc()
                    logger.critical("cannot recover from the following exception:\n" + tb)
                    for task in tasks:
                        logger.critical("tried to return task {0} from {1}".format(task.tag, task.hostname))
                    raise
            if successful_jobs >= abort_threshold and not abort_active:
                logger.info("activating fast abort with multiplier: {0}".format(abort_multiplier))
                abort_active = True
                queue.activate_fast_abort(abort_multiplier)

            # recurring actions are triggered here
            if actions:
                actions.take()
        if jobits_left == 0:
            logger.info("no more work left to do")
        if actions:
            actions.cleanup()
