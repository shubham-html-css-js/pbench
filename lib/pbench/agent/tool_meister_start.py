# -*- mode: python -*-

"""pbench-tool-meister-start - start the execution of the Tool Meister
sub-system.

There are two roles tool-meister-start plays:

  1. (optionally) orchestrate the creation of the instances of the Redis
     server, Tool Data Sink, and Tool Meisters

  2. execute the start sequence for the Tool Meister sub-system

The `--orchestrate=(create|existing)` command line parameter is used to control
the orchestration.  The default is to "create" the instances of the Redis
server, Tool Data Sink, and Tool Meisters.  If the user specifies "existing",
then the command assumes the `--redis-server` and `--tool-data-sink` parameters
will be provided to direct the command to the location of those instances.

The sequence of steps to execute the above behaviors is as follows:

   1. Loading tool group data for the requested tool group
      - This is the first step regardless so that the tool information can be
        validated, the number of TMs and their marching orders can be
        enumerated, and when orchestrating the TMs, the list of where those
        TMs are requested to run can be determined
   2. [orchestrate] Starting a Redis server
   3. Creating the Redis channel for the TDS to talk to the client
      - <prefix>-to-client
      - TDS publishes, a client subscribes
      - This is how the TDS reports back to a client the success or failure
        of requested actions
   4. Pushing the loaded tool group data and metadata into the Redis server
      for the TDS and all the TMs
   5. [orchestrate] Starting the local Tool Data Sink process
   6. [orchestrate] Starting all the local and remote Tool Meisters
   7. Waiting for the TDS to send a message reporting that it, and all the TMs,
      started
      - The TDS knows all the TMs that were started from the registered tools
        data structure argument given to it
   8. Verify all the requested Tool Meisters have reported back and that their
      tool installation checks were successful

There is a specific flow of data between these various components.  This
command, `pbench-tool-meister-start`, waits on the "<prefix>-to-client"
channel after starting the TDS and TMs.  The TDS is responsible for creating
and subscribing to the "<prefix>-from-tms" channel to wait for all the TMs to
report in.  The TMs create and subscribe to the "<prefix>-to-tms" channel
waiting for their first command.  The TMs then publish they are ready on the
"<prefix>-from-tms" channel. Once the TDS sees all the expected TMs, it writes
a set of combined metadata about all the TMs, along with the (optional)
external metadata passed to it on startup, to the local "metadata.log" file in
the "${benchmark_run_dir}".  It then tells this command the combined success /
failure of its startup and that of the TMs via the "<prefix>-to-client"
channel.

Summary of the other Redis pub/sub channels:

   1. "<prefix>-to-client" channel for the TDS to talk to a client
      - a client subscribes, the TDS publishes
      - This is used by the TDS to report success or failure of an action to
        a client

   2. "<prefix>-from-client" channel for a client to talk to the TDS
      - TDS subscribes, a client publishes
      - This is used to send the various actions from the client to the TDS

   3. "<prefix>-to-tms" channel for the TDS to talk to TMs
       - TMs subscribe, TDS publishes
       - This is how the TDS forwards actions to the TMs

   4. "<prefix>-from-tms" channel for TMs to talk to the TDS
       - TMs publish, TDS subscribes
       - This is how TMs tell the TDS the success or failure of their actions

Once a success message is received by this command from the TDS, the following
steps are taken as a normal client:

   1. Collect any requested system information ("sysinfo" action)
   2. Start any persistent tools running ("init" action)

When this command is orchestrating the creation of the Redis server and Tool
Data Sink instances, it will exit leaving those processes running in the
background, along with any local and/or remote Tool Meisters.

There are 4 environment variables required for execution as well:

  - pbench_install_dir
  - benchmark_run_dir
  - _pbench_hostname
  - _pbench_full_hostname

There are optional environment variables used to provide metadata about
the benchmark execution environment:

  - benchmark
  - config
  - date

There are also optional environment variables to affect the network operation
of the Pbench Tool Meister servers and clients:

  - ssh-opts
  - PBENCH_TOOL_DATA_SINK connection information for TDS
  - PBENCH_REDIS_SERVER connection information for Redis

        Both PBENCH_TOOL_DATA_SINK and PBENCH_REDIS_SERVER allow you to specify
        the host:port for the server. You can omit either the port or the host
        ("host" or ":port"), taking the hardcoded defaults. You can specify
        separate "connection" (for the clients) and "bind" (for the server)
        specifications by separating the bind specification and the connection
        specification (in that order) with a semicolon: e.g.,
        "bindhost:bindport;connectionhost:connectionport"

The environment variable _PBENCH_TOOL_MEISTER_START_LOG_LEVEL can be defined to
specify the logging level (e.g., _PBENCH_TOOL_MEISTER_START_LOG_LEVEL=debug);
by default only INFO, WARNING, and ERROR are included.

The pbench-tool-meister-start command will also propagate component Log level
environment variables when the tool data sink and remote tool meister instances
are created with `--orchestrate=create`:

    _PBENCH_TOOL_MEISTER_LOG_LEVEL      Remote Tool Meister client
    _PBENCH_TOOL_DATA_SINK_LOG_LEVEL    Tool Data Sink
"""

from argparse import ArgumentParser, Namespace
from distutils.spawn import find_executable
import ipaddress
import json
import logging
import os
from pathlib import Path
import shlex
import shutil
import socket
import sys
import time
from typing import Dict, Union
import uuid

import redis

from pbench.agent.constants import (
    cli_tm_channel_prefix,
    def_redis_port,
    def_wsgi_port,
    tm_channel_suffix_from_client,
    tm_channel_suffix_to_client,
    tm_channel_suffix_to_logging,
    tm_data_key,
)
from pbench.agent.redis_utils import RedisChannelSubscriber
from pbench.agent.tool_data_sink import main as tds_main
from pbench.agent.tool_group import BadToolGroup, ToolGroup
from pbench.agent.tool_meister import main as tm_main
from pbench.agent.tool_meister_client import Client
from pbench.agent.toolmetadata import ToolMetadata
from pbench.agent.utils import (
    BaseReturnCode,
    BaseServer,
    cli_verify_sysinfo,
    error_log,
    info_log,
    LocalRemoteHost,
    RedisServerCommon,
    TemplateSsh,
    warn_log,
)
from pbench.common.utils import Cleanup, validate_hostname

# The --orchestrate parameter default choice, and the full list of choices.
_orchestrate_choices = ["create", "existing"]
_def_orchestrate_choice = "create"
assert _def_orchestrate_choice in _orchestrate_choices, (
    f"The default orchestrate choice, {_def_orchestrate_choice!r},"
    f" is not one of the available choices, {_orchestrate_choices!r}"
)

# Wait at most 60 seconds for the Tool Data Sink to start listening on its
# logging sink channel.
_TDS_STARTUP_TIMEOUT = 60


class ReturnCode(BaseReturnCode):
    """ReturnCode - symbolic return codes for the main program of
    pbench-tool-meister-start.
    """

    BADTOOLGROUP = 1
    # Removed BADAGENTCONFIG = 2
    MISSINGBENCHRUNDIR = 3
    MISSINGINSTALLDIR = 4
    EXCINSTALLDIR = 5
    BADTOOLMETADATA = 6
    MISSINGREQENVS = 7
    EXCCREATETMDIR = 8
    MISSINGHOSTNAMEENVS = 9
    NOIP = 10
    EXCREDISCONFIG = 11
    EXCSPAWNREDIS = 12
    REDISFAILED = 13
    REDISCHANFAILED = 14
    REDISTMKEYFAILED = 15
    REDISTDSKEYFAILED = 16
    TDSFORKFAILED = 17
    TDSLOGPUBFAILED = 18
    TMFAILURES = 19
    EXCBENCHRUNDIR = 20
    TDSWAITFAILURE = 21
    EXCSYSINFODIR = 22
    EXCTOOLGROUPDIR = 23
    SYSINFOFAILED = 24
    INITFAILED = 25
    TDSSTARTUPTIMEOUT = 26
    TOOLGROUPEXC = 27
    BADREDISARG = 28
    BADREDISPORT = 29
    TMMISSING = 30
    BADWSGIPORT = 31
    BADSYSINFO = 32
    MISSINGPARAMS = 33
    MISSINGSSHCMD = 34
    BADWSGIHOST = 35
    BADREDISHOST = 36
    BADFULLHOSTNAME = 37
    BADHOSTNAME = 38
    INVALIDORCHESTRATE = 39
    REMOTENOTREACHABLE = 40
    KEYBOARDINTERRUPT = 41
    INVALIDTMDATA = 42
    TOOLINSTALLFAILURES = 43
    EXCCREATEUUID = 44


class CleanupTime(Exception):
    """
    Used to support handling errors during startup without constantly testing the
    current status and additional indentation. This will be raised to an outer
    try block when an error occurs.
    """

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message

    def __str__(self) -> str:
        return f"Cleanup requested with status {self.status}: {self.message}"


def _waitpid(pid: int) -> int:
    """Wrapper for os.waitpid()

    Returns the exit status of the given process ID.

    Raises an exception if the final exit PID is different from the given PID.
    """
    exit_pid, _exit_status = os.waitpid(pid, 0)
    assert pid == exit_pid, f"os.waitpid() returned pid {exit_pid}; expected {pid}"
    if os.WIFEXITED(_exit_status):
        return os.WEXITSTATUS(_exit_status)
    elif os.WIFSIGNALED(_exit_status):
        raise StartTmsErr(
            f"child process killed by signal {os.WTERMSIG(_exit_status)}",
            ReturnCode.TDSWAITFAILURE,
        )
    else:
        raise StartTmsErr(
            f"wait for child process returned unexpectedly, status = {_exit_status}",
            ReturnCode.TDSWAITFAILURE,
        )


class StartTmsErr(ReturnCode.Err):
    """StartTmsErr - derived from ReturnCode.Err, specifically raised by the
    start_tms_via_ssh() method.
    """

    pass


def start_tms_via_ssh(
    exec_dir: Path,
    ssh_cmd: str,
    tool_group: ToolGroup,
    ssh_opts: str,
    redis_server: RedisServerCommon,
    instance_uuid: str,
    logger: logging.Logger,
) -> None:
    """Orchestrate the creation of local and remote Tool Meister instances using
    ssh for those that are remote.

    Raises a StartTmsErr on failure.

    NOTE: all local and remote Tool Meisters are started even if failures
    occur for some; this allows the user to see logs for all the individual
    failures.
    """
    assert len(tool_group.hostnames) > 0, "No hosts to run tools"
    lrh = LocalRemoteHost()
    failures = 0
    successes = 0
    tool_meister_cmd = exec_dir / "tool-meister" / "pbench-tool-meister"
    debug_level = os.environ.get("_PBENCH_TOOL_MEISTER_LOG_LEVEL")
    cmd = f"{tool_meister_cmd} {redis_server.host} {redis_server.port} {{tm_param_key}} {instance_uuid} yes"
    if debug_level:
        cmd += f" {debug_level}"
    template = TemplateSsh(ssh_cmd, shlex.split(ssh_opts), cmd)
    tms: Dict[str, Union[str, int, Dict[str, str]]] = {}
    tm_count = 0
    for host in tool_group.hostnames.keys():
        tm_count += 1
        tm_param_key = f"tm-{tool_group.name}-{host}"
        if lrh.is_local(host):
            logger.debug("6a. starting localhost tool meister")
            try:
                pid = os.fork()
                if pid == 0:
                    # In the child!

                    # The main() of the Tool Meister module will not return
                    # here since it will daemonize itself and this child pid
                    # will be replaced by a new pid.
                    status = tm_main(
                        [
                            str(tool_meister_cmd),
                            redis_server.local_host,
                            str(redis_server.port),
                            tm_param_key,
                            instance_uuid,
                            "yes",  # Yes, daemonize yourself TM ...
                            debug_level,
                        ]
                    )
                    sys.exit(status)
                else:
                    # In the parent!
                    pass
            except Exception:
                logger.exception("failed to create localhost tool meister, daemonized")
                failures += 1
                tms[host] = {"status": "failed"}
            else:
                # Record the child pid to wait below.
                tms[host] = {"pid": pid, "status": "forked"}
        else:
            logger.debug("6b. starting remote tool meister on %s", host)
            try:
                template.start(host, tm_param_key=tm_param_key)
            except Exception:
                logger.exception(
                    "failed to create a tool meister instance for host %s", host
                )
                tms[host] = {"status": "failed"}
            else:
                # Record that the host command has spawned
                tms[host] = {"status": "spawned"}

    for host, tm_proc in tms.items():
        if tm_proc["status"] == "failed":
            failures += 1
            continue
        elif tm_proc["status"] == "forked":
            pid = tm_proc["pid"]
            try:
                exit_status = _waitpid(pid)
            except Exception:
                failures += 1
                logger.exception(
                    "failed to create a tool meister instance for host %s", host
                )
            else:
                if exit_status != 0:
                    failures += 1
                    logger.error(
                        "failed to start tool meister on local host host '%s'"
                        " (pid %d), exit status: %d",
                        host,
                        pid,
                        exit_status,
                    )
                else:
                    successes += 1
        elif tm_proc["status"] == "spawned":
            status = template.wait(host)
            if status.status != 0:
                failures += 1
                logger.error(
                    "failed to start tool meister on remote host '%s', exit status: %d",
                    host,
                    status.status,
                )
            else:
                successes += 1

    assert tm_count == len(tool_group.hostnames) and tm_count == (
        successes + failures
    ), f"Number of successes ({successes}) and failures ({failures}) for TM creation don't add up (should be {tm_count})"

    if failures > 0:
        raise StartTmsErr(
            "failures encountered creating Tool Meisters", ReturnCode.TMFAILURES
        )
    if successes != tm_count:
        raise StartTmsErr(
            f"number of created Tool Meisters, {successes}, does not"
            f" match the expected number of Tool Meisters, {tm_count}",
            ReturnCode.TMMISSING,
        )


class ToolDataSink(BaseServer):
    """ToolDataSink - an encapsulation of the handling of the Tool Data Sink
    specification and methods to optionally create and manage an instance.
    """

    def_port = def_wsgi_port
    bad_port_ret_code = ReturnCode.BADWSGIPORT
    bad_host_ret_code = ReturnCode.BADWSGIHOST
    name = "Tool Data Sink"

    def start(
        self,
        exec_dir: Path,
        tds_param_key: str,
        instance_uuid: str,
        redis_server: RedisServerCommon,
        redis_client: redis.Redis,
    ) -> None:
        assert (
            self.host is not None
            and self.port is not None
            and self.bind_host is not None
            and self.bind_port is not None
        ), f"Unexpected state: {self!r}"
        try:
            pid = os.fork()
            if pid == 0:
                # In the child!

                # The main() of the Tool Data Sink module will not return here
                # since it will daemonize itself and this child pid will be
                # replaced by a new pid.
                status = tds_main(
                    [
                        exec_dir / "tool-meister" / "pbench-tool-data-sink",
                        redis_server.local_host,
                        str(redis_server.port),
                        tds_param_key,
                        instance_uuid,
                        "yes",  # Request tool-data-sink daemonize itself
                        os.environ.get("_PBENCH_TOOL_DATA_SINK_LOG_LEVEL", "info"),
                    ]
                )
                sys.exit(status)
            else:
                # In the parent!

                # Wait for the child to finish daemonizing itself.
                retcode = _waitpid(pid)
                if retcode != 0:
                    raise self.Err(
                        f"failed to create pbench data sink, daemonized; return code: {retcode}",
                        ReturnCode.TDSWAITFAILURE,
                    )

        except Exception:
            raise self.Err(
                "failed to create tool data sink, daemonized", ReturnCode.TDSFORKFAILED
            )

        # Wait for logging channel to be up and ready before we start the
        # local and remote Tool Meisters.
        timeout = time.time() + _TDS_STARTUP_TIMEOUT
        num_present = 0
        while num_present == 0:
            try:
                num_present = redis_client.publish(
                    f"{cli_tm_channel_prefix}-{tm_channel_suffix_to_logging}",
                    "pbench-tool-meister-start - verify logging channel up",
                )
            except Exception:
                raise self.Err(
                    "Failed to verify Tool Data Sink logging sink working",
                    ReturnCode.TDSLOGPUBFAILED,
                )
            else:
                if num_present == 0:
                    if time.time() > timeout:
                        raise self.Err(
                            "The Tool Data Sink failed to start within one minute",
                            ReturnCode.TDSSTARTUPTIMEOUT,
                        )
                    else:
                        time.sleep(0.1)

        # TDS daemonization should create a PID file in the current working
        # directory; confirm it exists, and record the path for `kill`.
        pid_file = Path("pbench-tool-data-sink.pid").resolve()
        if pid_file.exists():
            self.pid_file = pid_file
        else:
            raise self.Err(
                f"TDS daemonization didn't create {pid_file}", ReturnCode.TDSWAITFAILURE
            )

    @staticmethod
    def wait(chan: RedisChannelSubscriber, logger: logging.Logger) -> int:
        """wait - Wait for the Tool Data Sink to report back success or
        failure regarding the Tool Meister environment setup.
        """
        status = ""
        for data in chan.fetch_json(logger):
            # We expect the payload to look like:
            #   { "kind": "ds",
            #     "action": "startup",
            #     "status": "success|failure"
            #   }
            try:
                kind = data["kind"]
                action = data["action"]
                status = data["status"]
            except KeyError:
                logger.warning("unrecognized data payload in message, '%r'", data)
                continue
            else:
                if kind != "ds":
                    logger.warning("unrecognized kind field in message, '%r'", data)
                    continue
                if action != "startup":
                    logger.warning("unrecognized action field in message, '%r'", data)
                    continue
                break
        return 0 if status == "success" else 1

    @staticmethod
    def is_running(pid: int) -> bool:
        """Is the given PID running?

        See https://stackoverflow.com/questions/7653178/wait-until-a-certain-process-knowing-the-pid-end

        Return True if a PID is running, else False if not.
        """
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    @staticmethod
    def wait_for_pid(pid: int) -> bool:
        """wait for a process to actually stop running, but eventually give up
        to avoid a hang."""
        running = True
        timeout = time.time() + _TDS_STARTUP_TIMEOUT
        while running:
            running = __class__.is_running(pid)
            if running:
                if time.time() >= timeout:
                    break
                time.sleep(0.1)
        return running

    def shutdown_tds(self, status: int) -> None:
        """Make sure TDS is shut down; wait for it to stop running on its own
        and kill it if the wait times out."""
        if self.wait_for_pid(self.get_pid()):
            self.kill(status)


class RedisServer(RedisServerCommon):
    """RedisServer - an encapsulation of the handling of the Redis server
    specification and methods to optionally create and manage an instance.
    """

    bad_port_ret_code = ReturnCode.BADREDISPORT
    bad_host_ret_code = ReturnCode.BADREDISHOST

    # Redis server configuration template for pbench's use
    conf_tmpl = """bind {bind_host_names}
daemonize yes
dir {tm_dir}
save ""
appendonly no
dbfilename pbench-redis.rdb
logfile {tm_dir}/redis.log
loglevel notice
pidfile {redis_pid_file}
port {redis_port:d}
"""

    def __init__(self, spec: str, def_host_name: str):
        super().__init__(spec, def_host_name)
        self.pid_file = None

    def start(self, tm_dir: Path) -> None:
        """start_redis - configure and start a Redis server.

        Raises a BaseServer.Err exception if an error is encountered.
        """
        assert (
            self.host is not None
            and self.port is not None
            and self.bind_host is not None
            and self.bind_port is not None
            and self.pid_file is None
        ), f"Unexpected state: {self!r}"

        try:
            bind_host_ip = socket.gethostbyname(self.bind_host)
        except socket.error as exc:
            raise self.Err(
                f"{self.bind_host} does not map to an IP address", ReturnCode.NOIP
            ) from exc
        else:
            assert (
                bind_host_ip is not None
            ), f"socket.gethostbyname('{self.bind_host}') returned None"
        try:
            host_ip = socket.gethostbyname(self.host)
        except socket.error as exc:
            raise self.Err(
                f"{self.host} does not map to an IP address", ReturnCode.NOIP
            ) from exc
        else:
            assert (
                host_ip is not None
            ), f"socket.gethostbyname('{self.host}') returned None"
            # By default, to talk to the Redis server locally, use the
            # specified host name.
            self.local_host = self.host

        bind_hostnames_l = [self.bind_host]
        # Determine if we can also use "localhost" to talk to the Redis server.
        if self.host != self.bind_host:
            # Somebody went through the trouble of telling us to bind to one
            # address and use another, so just do as we are told.
            pass
        elif self.bind_host == "0.0.0.0":
            # NOTE: we don't bother trying to determine multiple bind hosts.

            # Take advantage of the bind IP to have local connections use the
            # local IP address; hardcoded value avoids setups where "localhost"
            # is not setup (go figure).
            self.local_host = "127.0.0.1"
        else:
            # See if we can safely add "localhost" to the bind host name.  This
            # check is necessary because sometimes callers might have already
            # specified a name that maps to 127.0.0.1, and Redis will throw an
            # error if multiple names mapped to the same address.
            try:
                localhost_ip = socket.gethostbyname("localhost")
            except socket.error:
                # Interesting networking environment, no IP address for
                # "localhost".  Just use the host we already have.
                pass
            else:
                if bind_host_ip != localhost_ip:
                    assert (
                        self.bind_host != "localhost"
                    ), f"self.bind_host ({self.bind_host:r}) == 'localhost'?"
                    # The bind host name is not the same as "localhost" so we
                    # can add it to the list of host names the Redis server
                    # will bind to.
                    bind_hostnames_l.append("localhost")
                    self.local_host = "localhost"
                else:
                    # Whatever the self.bind_host is, it maps to the same IP
                    # address as localhost, so just use the self.host for any
                    # "local" access.
                    pass

        bind_host_names = " ".join(bind_hostnames_l)

        # Create the Redis server pbench-specific configuration file
        self.pid_file = tm_dir / "redis.pid"
        redis_conf = tm_dir / "redis.conf"
        params = {
            "bind_host_names": bind_host_names,
            "tm_dir": tm_dir,
            "redis_port": self.bind_port,
            "redis_pid_file": str(self.pid_file),
        }
        try:
            with redis_conf.open("w") as fp:
                fp.write(self.conf_tmpl.format(**params))
        except Exception as exc:
            raise self.Err(
                "failed to create redis server configuration", ReturnCode.EXCREDISCONFIG
            ) from exc

        # Start the Redis Server itself
        redis_srvr = "redis-server"
        redis_srvr_path = find_executable(redis_srvr)
        try:
            retcode = os.spawnl(os.P_WAIT, redis_srvr_path, redis_srvr, redis_conf)
        except Exception as exc:
            raise self.Err(
                "failed to create redis server, daemonized", ReturnCode.EXCSPAWNREDIS
            ) from exc
        else:
            if retcode != 0:
                raise self.Err(
                    f"failed to create redis server, daemonized; return code: {retcode:d}",
                    ReturnCode.REDISFAILED,
                )


def terminate_no_wait(
    tool_group_name: str, logger: logging.Logger, redis_client: redis.Redis, key: str
) -> None:
    """
    Use a low-level Redis publish operation to send a "terminate" request to
    all clients without waiting for a response. This is intended to be used
    as part of cleanup during unexpected termination, to make at least an
    attempt at clean termination before quitting. Success is not guaranteed,
    and we only check whether the message was sent.

    TODO: Ideally, we'd wait some reasonable time for a response from TDS and
        then quit; we don't have that mechanism, but this means we may kill a
        managed Redis instance before the requests propagate.

    Args:
        tool_group_name: The tool group we're trying to terminate
        logger: Python Logger
        redis_client: Redis client
        key: TDS Redis pubsub key
    """
    terminate_msg = {
        "action": "terminate",
        "group": tool_group_name,
        "directory": None,
        "args": {"interrupt": False},
    }
    try:
        ret = redis_client.publish(
            key,
            json.dumps(terminate_msg, sort_keys=True),
        )
    except Exception:
        logger.exception("Failed to publish terminate message")
    else:
        logger.debug("publish('terminate') = %r", ret)


def start(_prog: str, cli_params: Namespace) -> int:
    """Main program for tool meister start.

    :cli_params: expects a CLI parameters object which has five attributes:

        * orchestrate    - Keyword value of either "create" or "existing" to
                           indicate if tool meister start should create the
                           various instances of the Redis server, Tool Data
                           Sink, and Tool Meisters, or if it should expect to
                           use existing instances
        * redis_server   - The IP/port specification of the Redis server; when
                           'orchestrate' is "create", the value specifies the
                           IP/port the created Redis server will use; when it
                           is 'existing', the value specifies the IP/port to
                           use to connect to an existing instance
        * sysinfo        - The system information set to be collected during the
                           start sequence
        * tool_data_sink - The IP/port specification of the Tool Data Sink;
                           follows the same pattern as 'redis_server'
        * tool_group     - The tool group from which to load the registered tools


    Return 0 on success, non-zero ReturnCode class value on failure.
    """
    prog = Path(_prog)
    logger = logging.getLogger(prog.name)
    if os.environ.get("_PBENCH_TOOL_MEISTER_START_LOG_LEVEL") == "debug":
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO
    logger.setLevel(log_level)
    sh = logging.StreamHandler()
    sh.setLevel(log_level)
    shf = logging.Formatter(f"{prog.name}: %(message)s")
    sh.setFormatter(shf)
    logger.addHandler(sh)
    tm_dir = None
    ssh_cmd = None

    # +
    # Step 1. - Load the tool group data for the requested tool group
    # -

    # Verify all the command line arguments
    try:
        # Load the tool group data
        tool_group = ToolGroup(cli_params.tool_group)
    except BadToolGroup as exc:
        logger.error(str(exc))
        return ReturnCode.BADTOOLGROUP
    except Exception:
        logger.exception(
            "failed to load tool group data for '%s'", cli_params.tool_group
        )
        return ReturnCode.TOOLGROUPEXC
    else:
        if not tool_group.hostnames:
            # If a tool group has no tools registered, then there will be no
            # host names on which to start Tool Meisters.
            return ReturnCode.SUCCESS

    sysinfo, bad_l = cli_verify_sysinfo(cli_params.sysinfo)
    if bad_l:
        logger.error("invalid sysinfo option(s), '%s'", ",".join(bad_l))
        return ReturnCode.BADSYSINFO

    if cli_params.orchestrate not in _orchestrate_choices:
        logger.error(
            "invalid --orchestrate directive, '%s', expected one of %s",
            cli_params.orchestrate,
            ", ".join(_orchestrate_choices),
        )
        return ReturnCode.INVALIDORCHESTRATE
    if cli_params.orchestrate == "existing":
        if not cli_params.redis_server or not cli_params.tool_data_sink:
            logger.error(
                "both --redis-server and --tool-data-sink must be specified"
                " if --orchestrate=%s is used",
                cli_params.orchestrate,
            )
            return ReturnCode.MISSINGPARAMS
        orchestrate = False
    else:
        orchestrate = True

    # Load and verify required and optional environment variables.
    try:
        inst_dir = os.environ["pbench_install_dir"]
        benchmark_run_dir_val = os.environ["benchmark_run_dir"]
        hostname = os.environ["_pbench_hostname"]
        full_hostname = os.environ["_pbench_full_hostname"]
    except KeyError as exc:
        logger.error("failed to fetch required environment variable, '%s'", exc.args[0])
        return ReturnCode.MISSINGREQENVS
    try:
        tm_start_path = Path(inst_dir).resolve(strict=True)
    except FileNotFoundError:
        logger.error(
            "Unable to determine proper installation directory, '%s' not found",
            inst_dir,
        )
        return ReturnCode.MISSINGINSTALLDIR
    except Exception as exc:
        logger.error(
            "Unexpected error encountered resolving installation directory, %s", exc
        )
        return ReturnCode.EXCINSTALLDIR
    if not full_hostname or not hostname:
        logger.error(
            "_pbench_hostname ('%s') and _pbench_full_hostname ('%s')"
            " environment variables are required to represent the respective"
            " hostname strings ('hostname -s' and 'hostname -f')",
            hostname,
            full_hostname,
        )
        return ReturnCode.MISSINGHOSTNAMEENVS
    if validate_hostname(full_hostname) != 0:
        logger.error("Invalid _pbench_full_hostname, '%s'", full_hostname)
        return ReturnCode.BADFULLHOSTNAME
    if validate_hostname(hostname) != 0:
        logger.error("Invalid _pbench_hostname, '%s'", hostname)
        return ReturnCode.BADHOSTNAME
    try:
        benchmark_run_dir = Path(benchmark_run_dir_val).resolve(strict=True)
    except FileNotFoundError:
        logger.error(
            "benchmark_run_dir directory, '%s', does not exist", benchmark_run_dir_val
        )
        return ReturnCode.MISSINGBENCHRUNDIR
    except Exception as exc:
        logger.error(
            "an unexpected error occurred resolving benchmark_run_dir"
            " directory, '%s': %s",
            benchmark_run_dir_val,
            exc,
        )
        return ReturnCode.EXCBENCHRUNDIR
    if orchestrate:
        tm_dir = benchmark_run_dir / "tm"
        try:
            tm_dir.mkdir()
            # All orchestration occurs using the newly created Tool Meister
            # directory as the current working directory.
            os.chdir(tm_dir)
        except Exception as exc:
            logger.error(
                "failed to create the local tool meister directory, '%s': %s",
                tm_dir,
                exc,
            )
            return ReturnCode.EXCCREATETMDIR
        uuid_file = tm_dir / ".uuid"
        instance_uuid = str(uuid.uuid4())
        try:
            uuid_file.write_text(instance_uuid)
        except Exception as exc:
            logger.error(
                "failed to create a UUID in '%s': %s",
                uuid_file,
                exc,
            )
            return ReturnCode.EXCCREATEUUID

    # See if anybody told us to use certain options with SSH commands.
    ssh_opts = os.environ.get("ssh_opts", "")

    # Load optional metadata environment variables
    optional_md = dict(
        script=os.environ.get("benchmark", ""),
        config=os.environ.get("config", ""),
        date=os.environ.get("date", ""),
        ssh_opts=ssh_opts,
    )

    redis_server_spec = cli_params.redis_server
    tds_server_spec = cli_params.tool_data_sink

    recovery = Cleanup(logger)

    try:
        if orchestrate:
            ssh_cmd = shutil.which("ssh")
            if ssh_cmd is None:
                raise CleanupTime(
                    ReturnCode.MISSINGSSHCMD, "required ssh command not in our PATH"
                )

            # Connect with each of the tool hosts: if we can't connect via ssh,
            # this isn't going to work, so if any of them fails we'll terminate
            # with an error code.
            #
            # If we can connect, they'll tell us the IP address from which they see
            # us connecting, and we'll use that (by default) as the server address.
            localhost = LocalRemoteHost()
            origin_ip = set()
            any_remote = False
            template = TemplateSsh(
                ssh_cmd, shlex.split(ssh_opts), "echo ${SSH_CONNECTION}"
            )
            recovery.add(template.abort, "stop TM clients")

            for host in tool_group.hostnames.keys():
                if not localhost.is_local(host):
                    any_remote = True
                    template.start(host)

            for host, params in tool_group.hostnames.items():
                if not localhost.is_local(host):
                    connection = template.wait(host)
                    logger.debug("Host %s reports connection `%s`", host, connection)
                    if connection.status == 0 and connection.stdout:
                        # The SSH_CONNECTION value is the full `stdout` and we
                        # don't expect a stderr on success; the format is
                        #   "origin_node origin_port local_node local_port"
                        # we only need the origin node because we know that's an
                        # IP for our local TDS and Redis host that the remote can
                        # reach.
                        origin = connection.stdout.split()[0]
                        origin_ip.add(origin)

                        # Store the origin reported by this remote in its parameter
                        # block: we'll program this as the connection address when
                        # we send the init handshake to that remote later.
                        params["origin_host"] = origin
                    else:
                        # If `ssh` fails, we can't orchestrate anything on this
                        # remote, so terminate with an error.
                        raise CleanupTime(
                            ReturnCode.REMOTENOTREACHABLE,
                            f"Host {host} reports {connection}",
                        )

            # Process the collected origin addresses from our remotes. Ideally we
            # have only a single entry here, since the current BaseServer init
            # (and the Bottle WSGI server) doesn't support listening on multiple
            # addresses; however we're going to just take the first returned origin
            # address and hope they can all connect.
            #
            # NOTE: an alternative would be (in the absence of an explicit
            # connection spec) to always listen on '0.0.0.0', but this eliminates
            # IPv6, and we currently lack the mechanism to listen on both '0.0.0.0'
            # and '::' (the IPv6 equivalent, aka '0:0:0:0:0:0:0:0').
            #
            # NOTE: If the caller/human supplied explicit PBENCH_REDIS_SERVER or
            # PBENCH_TOOL_DATA_SINK (--redis-server or --tool-data-sink), we'll use
            # those, but by default we'll use the ssh connection origin instead of
            # the local `hostname` which may not be reachable by the remotes (or
            # necessarily routable at all).
            if len(origin_ip) != 1 and any_remote:
                logger.warning(
                    "Remote hosts don't agree on a single controller "
                    "origin IP, which may indicate a problem: origin(s) %s",
                    ",".join(origin_ip) if origin_ip else "<none>",
                )
            if len(origin_ip) > 0:
                logger.debug("Our connection host(s): %s", ",".join(origin_ip))
                ip = ipaddress.ip_address(next(iter(origin_ip)))
                if isinstance(ip, ipaddress.IPv6Address):
                    origin = f"[{str(ip)}]"
                else:
                    origin = str(ip)
                if not tds_server_spec:
                    tds_server_spec = origin
                if not redis_server_spec:
                    redis_server_spec = origin

        # NOTE: These two assignments create server objects, but neither
        # constructor starts a server, so no cleanup action is needed at this
        # time.
        try:
            redis_server = RedisServer(redis_server_spec, full_hostname)
        except RedisServer.Err as exc:
            raise CleanupTime(exc.return_code, str(exc))

        try:
            tool_data_sink = ToolDataSink(tds_server_spec, full_hostname)
        except ToolDataSink.Err as exc:
            raise CleanupTime(exc.return_code, str(exc))

        # Load the tool metadata
        try:
            tool_metadata = ToolMetadata(tm_start_path)
        except Exception:
            raise CleanupTime(
                ReturnCode.BADTOOLMETADATA, "failed to load tool metadata"
            )

        # +
        # Step 2. - Start the Redis Server (optional)
        # -

        if orchestrate:
            logger.debug("2. starting redis server")
            try:
                redis_server.start(tm_dir)
            except redis_server.Err as exc:
                raise CleanupTime(
                    exc.return_code, f"Failed to start a local Redis server: '{exc}'"
                )

            # NOTE: The `kill` method is designed to modify a "base" return
            # value with an additional kill status. This would require a lot
            # of extra cleanup logic to bind the parameter at cleanup time, and
            # then capture the returned values from the queued cleanup actions
            # and somehow combine them into a single integer. It seems unlikely
            # this would be useful.
            recovery.add(
                lambda: redis_server.kill(ReturnCode.KEYBOARDINTERRUPT), "stop Redis"
            )

        # +
        # Step 3. - Creating the Redis channel for the TDS to talk to the client
        # -

        # It is not sufficient to just create the Redis() object, we have to
        # initiate some operation with the Redis Server. We use the creation of the
        # "<prefix>-to-client" channel for that purpose. We'll be acting as a
        # client later on, so we subscribe to the "<prefix>-to-client" channel to
        # listen for responses from the Tool Data Sink.
        logger.debug("3. connecting to the redis server")
        try:
            redis_client = redis.Redis(
                host=redis_server.host, port=redis_server.port, db=0
            )
            to_client_chan = RedisChannelSubscriber(
                redis_client, f"{cli_tm_channel_prefix}-{tm_channel_suffix_to_client}"
            )
        except Exception as exc:
            raise CleanupTime(
                ReturnCode.REDISCHANFAILED,
                f"Unable to connect to redis server, {redis_server}: {exc}",
            )

        # +
        # Step 4. - Push the loaded tool group data and metadata into the Redis
        #           server
        # -

        logger.debug("4. push tool group data and metadata")

        # First we copy the entire directory hierarchy to the benchmark run
        # directory. We do this as part of the start-up since we don't want to
        # rely on the Tool Data Sink for recording the processed version of
        # this data in the metadata.log file.  We need to have this on hand to
        # determine what the inputs were to the start operation.
        tool_group.archive(benchmark_run_dir)

        tool_group_data = dict()
        for host, params in tool_group.hostnames.items():
            tools = tool_group.get_tools(host)
            tm = dict(
                benchmark_run_dir=str(benchmark_run_dir),
                channel_prefix=cli_tm_channel_prefix,
                tds_hostname=params["origin_host"]
                if "origin_host" in params
                else tool_data_sink.host,
                tds_port=tool_data_sink.port,
                controller=full_hostname,
                tool_group=tool_group.name,
                hostname=host,
                label=tool_group.get_label(host),
                tool_metadata=tool_metadata.getFullData(),
                tools=tools,
                instance_uuid=instance_uuid,
            )
            # Create a separate key for the Tool Meister that will be on that host
            tm_param_key = f"tm-{tool_group.name}-{host}"
            try:
                redis_client.set(tm_param_key, json.dumps(tm, sort_keys=True))
            except Exception:
                raise CleanupTime(
                    ReturnCode.REDISTMKEYFAILED,
                    "failed to create tool meister parameter key in redis server",
                )
            tool_group_data[host] = tools

            recovery.add(
                lambda: redis_client.delete(tm_param_key), f"delete {host} Redis key"
            )

        # Create the key for the Tool Data Sink
        tds_param_key = f"tds-{tool_group.name}"
        tds = dict(
            benchmark_run_dir=str(benchmark_run_dir),
            bind_hostname=tool_data_sink.bind_host,
            port=tool_data_sink.bind_port,
            channel_prefix=cli_tm_channel_prefix,
            tool_group=tool_group.name,
            tool_metadata=tool_metadata.getFullData(),
            tool_trigger=tool_group.trigger,
            tools=tool_group_data,
            instance_uuid=instance_uuid,
            # The following are optional
            optional_md=optional_md,
        )
        try:
            redis_client.set(tds_param_key, json.dumps(tds, sort_keys=True))
        except Exception:
            raise CleanupTime(
                ReturnCode.REDISTDSKEYFAILED,
                "failed to create tool data sink parameter key in redis server",
            )

        recovery.add(lambda: redis_client.delete(tds_param_key), "delete TDS key")

        # +
        # Step 5. - Start the Tool Data Sink process (optional)
        # -

        if orchestrate:
            logger.debug("5. starting tool data sink")
            try:
                tool_data_sink.start(
                    prog.parent,
                    tds_param_key,
                    instance_uuid,
                    redis_server,
                    redis_client,
                )
            except tool_data_sink.Err as exc:
                raise CleanupTime(
                    exc.return_code, f"failed to start local tool data sink, '{exc}'"
                )

            recovery.add(
                lambda: tool_data_sink.shutdown_tds(ReturnCode.KEYBOARDINTERRUPT),
                "stop TDS",
            )

        # We haven't yet started any remote clients; however, if we
        # terminate during the startup sequence (via interrupt or error), we
        # need to tell the TDS to shut everything down if possible before we
        # kill it.
        #
        # We do this even if we aren't orchestrating the remote clients. We
        # don't "own" them, and won't explicitly terminate them; but we're
        # assigning work to them and the terminate message will tell them to
        # stop.
        recovery.add(
            lambda: terminate_no_wait(
                tool_group.name,
                logger,
                redis_client,
                f"{cli_tm_channel_prefix}-{tm_channel_suffix_from_client}",
            ),
            "terminate tool group",
        )

        # +
        # Step 6. - Start all the local and remote Tool Meisters (optional)
        # -

        if orchestrate:
            try:
                start_tms_via_ssh(
                    prog.parent,
                    ssh_cmd,
                    tool_group,
                    ssh_opts,
                    redis_server,
                    instance_uuid,
                    logger,
                )
            except StartTmsErr as exc:
                raise CleanupTime(
                    exc.return_code,
                    f"Failed to start all remote clients in {tool_group.name}",
                )

        # +
        # Step 7. - Wait for the TDS to send a message reporting that it, and all
        #           the TMs, started.
        # -

        # Note that this is not related to orchestration. If the caller
        # provided their own Redis server, implying they started their own Tool
        # Data Sink and their own Tool Meisters, they still report back to us
        # because we provided their operational keys.
        #
        # If any succeed, then we need to wait for them to show up as
        # subscribers.
        logger.debug(
            "7. waiting for all successfully created Tool Meister processes"
            " to show up as subscribers"
        )
        ret_val = tool_data_sink.wait(to_client_chan, logger)
        if ret_val != 0:
            raise CleanupTime(
                ReturnCode.TDSWAITFAILURE, "TDS didn't confirm init sequence completion"
            )

        # +
        # Step 8. - Verify all the Tool Meisters have reported back, and that
        #           their tool install checks all passed.
        # -
        try:
            tms = json.loads(redis_client.get(tm_data_key))
        except Exception as exc:
            error_log(f"Error loading operational Tool Meister data, '{exc}'")
            raise CleanupTime(
                ReturnCode.INVALIDTMDATA,
                "Failed to load reported Tool Meister operational data",
            )

        tool_install_failures = {}
        for host, tm in tms.items():
            if not tm["failed_tools"]:
                continue
            tool_install_failures[host] = tm

        if tool_install_failures:
            error_log("Tool installation checks failed")
            for host, tm in tool_install_failures.items():
                for tool_name, (failure_code, output) in tm["installs"].items():
                    if failure_code != 0:
                        error_log(
                            f"{host}: {tool_name} return code: {failure_code},"
                            f" output: '{output}'"
                        )
            raise CleanupTime(
                ReturnCode.TOOLINSTALLFAILURES,
                "Tool installation check failures encountered",
            )

        # Setup a Client API object using our existing to_client_chan object to
        # drive the following client operations ("sysinfo" [optional] and "init"
        # [required]).
        with Client(
            redis_server=redis_client,
            channel_prefix=cli_tm_channel_prefix,
            to_client_chan=to_client_chan,
            logger=logger,
        ) as client:
            if sysinfo:
                sysinfo_path = benchmark_run_dir / "sysinfo" / "beg"
                try:
                    sysinfo_path.mkdir(parents=True)
                except Exception:
                    error_log(
                        f"Unable to create sysinfo-dump directory base path: {sysinfo_path}"
                    )
                else:
                    logger.debug("7a. Collecting system information")
                    info_log("Collecting system information")
                    # Collecting system information is optional, so we don't gate
                    # the success or failure of the startup on it.
                    client.publish(tool_group.name, sysinfo_path, "sysinfo", sysinfo)

            tool_dir = benchmark_run_dir / f"tools-{tool_group.name}"
            try:
                tool_dir.mkdir(exist_ok=True)
            except Exception as exc:
                error_log(
                    f"failed to create tool output directory, '{tool_dir}': {exc}"
                )
                raise CleanupTime(
                    ReturnCode.EXCTOOLGROUPDIR, "Unable to create tool dir {tool_dir}"
                )
            else:
                recovery.add(lambda: shutil.rmtree(tool_dir), "delete tool directory")
                logger.debug("8. Initialize persistent tools")
                ret_val = client.publish(tool_group.name, tool_dir, "init", None)
                if ret_val != 0:
                    raise CleanupTime(
                        ReturnCode.INITFAILED, "Persistent tool initialization failed"
                    )
        return ret_val
    except KeyboardInterrupt:
        warn_log("Interrupted by user")
        recovery.cleanup()
        return ReturnCode.KEYBOARDINTERRUPT
    except CleanupTime as e:
        cause = e.__cause__ if e.__cause__ else e.__context__
        if e.status == ReturnCode.INITFAILED:
            log_func = logger.exception if cause else logger.error
            log_func("error %s", e.message)
        else:
            _cause_msg = f" ({cause})" if cause else ""
            warn_log(f"{e.message}{_cause_msg}")
        recovery.cleanup()
        return e.status
    except Exception:
        logger.exception("Unexpected exception in outer try")
        recovery.cleanup()
        return ReturnCode.INITFAILED


_NAME_ = "pbench-tool-meister-start"


def main():
    parser = ArgumentParser(f"{_NAME_} [--sysinfo <list of system information items>]")
    parser.add_argument(
        "--sysinfo",
        dest="sysinfo",
        default=None,
        help="The list of system information items to be collected.",
    )
    parser.add_argument(
        "--orchestrate",
        dest="orchestrate",
        default=os.environ.get("PBENCH_ORCHESTRATE", _def_orchestrate_choice),
        choices=_orchestrate_choices,
        help=(
            "The `create` keyword directs the command to create the various"
            " instances of the Redis server, Tool Data Sink, and Tool"
            " Meisters, while the `existing` keyword directs the command to"
            " use existing instances of all three. The default is `create`."
        ),
    )
    parser.add_argument(
        "--redis-server",
        dest="redis_server",
        default=os.environ.get("PBENCH_REDIS_SERVER", None),
        help=(
            "Specifies the IP/port to use for the Redis server - if not"
            " present, the defaults are used, ${_pbench_full_hostname}:"
            f"{def_redis_port};"
            " the specified value can take either of two forms:"
            " `<bind host>:<port>;<host>:<port>`, a semi-colon separated"
            " IP/port specified for both how the Redis server will bind"
            " itself, and how clients will connect; `<host>:<port>`, the"
            " IP/port combination is used both for binding and connecting"
            " (NOTE: binding is not used with --orchestrate=existing);"
        ),
    )
    parser.add_argument(
        "--tool-data-sink",
        dest="tool_data_sink",
        default=os.environ.get("PBENCH_TOOL_DATA_SINK", None),
        help=(
            "Specifies the IP/port to use for the Tool Data Sink - if not"
            " present, the defaults are used, ${_pbench_full_hostname}:"
            f"{def_wsgi_port};"
            " the specified value can take either of two forms:"
            " `<bind host>:<port>;<host>:<port>`, a semi-colon separated"
            " IP/port specified for both how the Tool Data Sink will bind"
            " itself, and how clients will connect; `<host>:<port>`, the"
            " IP/port combination is used both for binding and connecting"
            " (NOTE: binding is not used with --orchestrate=existing);"
        ),
    )
    parser.add_argument(
        "tool_group",
        help="The tool group name of tools to be run by the Tool Meisters.",
    )
    parsed = parser.parse_args()
    return start(sys.argv[0], parsed)
