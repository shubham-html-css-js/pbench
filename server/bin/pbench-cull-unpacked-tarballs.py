#!/usr/bin/env python3
# -*- mode: python -*-

"""Pbench Cull Unpacked Tar Balls

The maximum age of an unpacked tar ball is provided via the "unpacked-age"
variable in the [pbench-server] configuration setting.  By default we only
keep around unpacked tar balls for 30 days.

An unpacked tar ball directory can be kept around longer than the maximum
configured age by creating a ".__pbench_keep__" file in the top level of that
directory.

The culling of unpacked tar balls occurrs once a day. Each unpacked tar ball
found in the ${INCOMING} directory hierarchy is checked against the configured
maximum age, and removed (along with its ${RESULTS} and ${USERS} hierarchy
links).

"""

from argparse import ArgumentParser
import configparser
from datetime import datetime
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

from pbench.common import MetadataLog
from pbench.common.exceptions import BadConfig
from pbench.common.logger import get_pbench_logger
import pbench.server
from pbench.server.database import init_db
from pbench.server.indexer import _STD_DATETIME_FMT
from pbench.server.report import Report

_NAME_ = "pbench-cull-unpacked-tarballs"

tb_pat_r = r"\S+_(\d\d\d\d)[._-](\d\d)[._-](\d\d)[T_](\d\d)[._:](\d\d)[._:](\d\d)"
tb_pat = re.compile(tb_pat_r)


class Action:
    """Action - A simple class to track individual actions taken towards the
    removal of a unpacked tar ball.

    An action offers two fields: a "verb" for the action taken on a "noun".
    """

    def __init__(self, verb, noun):
        self.verb = verb
        self.noun = noun
        self.status = "n/a"

    def set_status(self, status):
        assert status in (
            "succ",
            "fail",
            "part",
        ), f"Unrecognized status value, {status}"
        self.status = status


class ActionSet:
    """ActionSet - A simple class to track a set of actions taken, how many
    errors occurred, when they started and when they stopped.
    """

    def __init__(self, actions, errors, start, end):
        self.actions = actions
        self.errors = errors
        self.start = start
        self.end = end
        self.name = ""

    def set_name(self, name):
        self.name = name

    def duration(self):
        return self.end - self.start


def fetch_username(tb_incoming_dir):
    """fetch_username - Return the pbench "run"'s "user" value from the
    `metadata.log` file, if it exists.
    """
    config = MetadataLog()
    try:
        config.read(Path(tb_incoming_dir, "metadata.log"))
    except Exception:
        return None
    try:
        user = config["run"].get("user")
    except Exception:
        return None
    return user


def remove_symlinks(tgt_p, tb_incoming_dir, logger, dry_run):
    """remove_symlinks - Given a target directory tree, remove all symbolic
    links which point to the given incoming tar ball directory.

    Any errors encountered will be logged.

    Returns a tuple consisting of the number of errors encountered and a list
    of 'Action's taken.
    """
    errors = 0
    actions_taken = []
    for dir_n, subdir_l, file_l in os.walk(tgt_p):
        # NOTE: we ignore any files found, as pbench-audit-server should flag
        # subject objects.
        for subdir_n in subdir_l:
            full_p = Path(dir_n, subdir_n)
            try:
                link = os.readlink(full_p)
            except OSError:
                continue
            if os.path.realpath(link) == tb_incoming_dir:
                act = Action("rm", str(full_p))
                # FIXME: Remember the path to the removed symbolic link, and
                # remove any parent directories that become empty as a result
                # of the symbolic link removal.
                if not dry_run:
                    try:
                        os.unlink(full_p)
                    except OSError as exc:
                        logger.error("Failed to remove symlink '{}': {}", full_p, exc)
                        errors += 1
                        status = "fail"
                    else:
                        logger.debug("Removed symlink '{}'", full_p)
                        status = "succ"
                    act.set_status(status)
                # Dry-run, or actual, errors or no errors, we always record
                # the action as taken for reporting purposes.
                actions_taken.append(act)
    # FIXME: By removing any symlinks, the directory containing that symlink
    # may now be empty.  And it could be the entire prefix chain could be
    # removed.  However, we don't want to remove prefixes, users, or
    # controllers at this point since that might create a race condition for
    # unpack tar balls (unpacking of a tar ball with a result prefix hierarchy
    # which matches another tar being removed from the same controller).
    return errors, actions_taken


def remove_unpacked(tb_incoming_dir, controller_name, results, users, logger, dry_run):
    """remove_unpacked - Remove the unpacked tar ball directory from the
    INCOMING tree and all symbolic links to that directory from the RESULTS
    and USERS trees.

    The symbolic links in the RESULTS and USERS (if any) trees are removed
    first, then the directory in the INCOMING tree is removed.

    The `tb_incoming_dir` should be a fully resolved path including the tar
    ball directory name, allowing us to resolve the symbolic link being
    considered to compare for equality.

    Any errors encountered while removing items will result in those errors
    being logged.  All removals stop on the first error encountered, so the
    removal process could leave the unpacked tar ball in a partially deleted
    state.

    Returns 0 on success, > 0 if any errors encountered.
    """
    start = pbench.server._time()
    if not dry_run:
        logger.info("Began removing unpacked tar ball directory, '{}'", tb_incoming_dir)

    # Walk the results hierarchy for the tar ball's controller to find all
    # symbolic links to the INCOMING directory location.  Once we find a
    # symbolic link (which will be returned in the list of sub-directories)
    errors, actions_taken = remove_symlinks(
        Path(results, controller_name), tb_incoming_dir, logger, dry_run
    )
    if errors > 0:
        # NOTE: dry-runs never produce errors, so no check is needed.
        logger.error(
            "Aborting removal of unpacked tar ball directory, '{}'", tb_incoming_dir
        )
        return ActionSet(actions_taken, errors, start, pbench.server._time())

    # Fetch user from metadata.log file.
    user_name = fetch_username(tb_incoming_dir)
    if user_name:
        errors, _actions_taken = remove_symlinks(
            Path(users, user_name, controller_name),
            tb_incoming_dir,
            logger,
            dry_run,
        )
        actions_taken.extend(_actions_taken)
        if errors > 0:
            # NOTE: dry-runs never produce errors, so no check is needed.
            logger.error(
                "Aborting removal of unpacked tar ball directory, '{}'", tb_incoming_dir
            )
            return ActionSet(actions_taken, errors, start, pbench.server._time())

    # Now that all RESULTS symbolic links and any USERS symbolic links to the
    # incoming directory have been removed, we can rename the directory (so it
    # is no longer visible to the web server) and then delete it.
    tb_incoming_dir = Path(tb_incoming_dir)
    del_path = Path(tb_incoming_dir.parent, f".delete.{tb_incoming_dir.name}")

    act = Action("mv", str(tb_incoming_dir))
    actions_taken.append(act)
    try:
        # First rename it to a temporary name so that it is immediately no
        # longer visable to the web server.
        if not dry_run:
            os.rename(tb_incoming_dir, del_path)
    except OSError as exc:
        logger.error(
            "Failed to rename incoming directory, '{}', to '{}': '{}'",
            tb_incoming_dir,
            del_path,
            exc,
        )
        errors = 1
        act.set_status("fail")
    else:
        act.set_status("succ")
        act = Action("rmtree", str(del_path))
        actions_taken.append(act)
        try:
            if not dry_run:
                shutil.rmtree(del_path)
        except OSError as exc:
            logger.error(
                "Failed to remove incoming directory tree, '{}': '{}'",
                del_path,
                exc,
            )
            errors = 1
            act.set_status("fail")
        else:
            act.set_status("succ")

    end = pbench.server._time()
    act_set = ActionSet(actions_taken, errors, start, end)
    if errors == 0:
        duration = 0.0 if end < start else end - start
        logger.info(
            "After {:0.2f} seconds, finished removal of unpacked tar ball"
            " directory, '{}'",
            duration,
            tb_incoming_dir,
        )
    return act_set


def gen_list_unpacked_aged(incoming, archive, curr_dt, max_unpacked_age):
    """gen_list_unpacked_aged - traverse the given INCOMING hierarchy looking
    for all tar balls whose "age" (as calculated from the date stamp in the tar
    ball ename) is older than the given maximum unpacked age (in days).

    An unpacked tar ball directory can be kept longer than the maximum age by
    creating a ".__pbench_keep__" file in the top level directory.

    NOTE: We are given the source time stamp to compare against to avoid
    drifting since the operation can take time.
    """
    with os.scandir(incoming) as incoming_scan:
        for c_entry in incoming_scan:
            if c_entry.name.startswith(".") and c_entry.is_dir(follow_symlinks=False):
                continue
            if not c_entry.is_dir(follow_symlinks=False):
                # NOTE: the pbench-audit-server should pick up and flag this
                # unwanted condition.
                continue
            # We have a controller directory.
            with os.scandir(c_entry.path) as controller_scan:
                for entry in controller_scan:
                    if entry.name.startswith(".") and entry.is_dir(
                        follow_symlinks=False
                    ):
                        continue
                    if not entry.is_dir(follow_symlinks=False):
                        # NOTE: the pbench-audit-server should pick up and
                        # flag this unwanted condition.
                        continue
                    match = tb_pat.fullmatch(entry.name)
                    if not match:
                        # Does not appear to be a valid tar ball directory
                        # name.
                        # NOTE: the pbench-audit-server should pick up and
                        # flag this unwanted condition.
                        continue
                    # We have a tar ball directory name, validate it.
                    tb_path = Path(archive, c_entry.name, f"{entry.name}.tar.xz")
                    if not tb_path.exists():
                        # NOTE: the pbench-audit-server should pick up and
                        # flag this unwanted condition.
                        continue
                    # Turn the pattern components of the match into a datetime
                    # object.
                    tb_dt = datetime(
                        int(match.group(1)),
                        int(match.group(2)),
                        int(match.group(3)),
                        int(match.group(4)),
                        int(match.group(5)),
                        int(match.group(6)),
                    )
                    # See if this unpacked tar ball directory has aged out.
                    timediff = curr_dt - tb_dt
                    if timediff.days > max_unpacked_age:
                        # Finally, make one last check to see if this tar ball
                        # directory should be kept regardless of aging out.
                        if Path(entry.path, ".__pbench_keep__").is_file():
                            continue
                        yield entry.path, c_entry.name


def main(options):
    if not options.cfg_name:
        print(
            f"{_NAME_}: ERROR: No config file specified; set"
            " _PBENCH_SERVER_CONFIG env variable",
            file=sys.stderr,
        )
        return 1

    try:
        config = pbench.server.PbenchServerConfig(options.cfg_name)
    except BadConfig as e:
        print(f"{_NAME_}: {e}", file=sys.stderr)
        return 2

    logger = get_pbench_logger(_NAME_, config)

    # We're going to need the Postgres DB to track dataset state, so setup
    # DB access.
    init_db(config, logger)

    archivepath = config.ARCHIVE

    incoming = config.INCOMING
    incomingpath = config.get_valid_dir_option("INCOMING", incoming, logger)
    if not incomingpath:
        return 3

    results = config.RESULTS
    resultspath = config.get_valid_dir_option("RESULTS", results, logger)
    if not resultspath:
        return 3

    users = config.USERS
    userspath = config.get_valid_dir_option("USERS", users, logger)
    if not userspath:
        return 3

    # Fetch the configured maximum number of days a tar can remain "unpacked"
    # in the INCOMING tree.
    try:
        max_unpacked_age = config.conf.get("pbench-server", "max-unpacked-age")
    except configparser.NoOptionError as e:
        logger.error(f"{e}")
        return 5
    try:
        max_unpacked_age = int(max_unpacked_age)
    except Exception:
        logger.error("Bad maximum unpacked age, {}", max_unpacked_age)
        return 6

    # First phase is to find all the tar balls which are beyond the max
    # unpacked age, and which still have an unpacked directory in INCOMING.
    if config._ref_datetime is not None:
        try:
            curr_dt = config._ref_datetime
        except Exception:
            # Ignore bad dates from test environment.
            curr_dt = datetime.utcnow()
    else:
        curr_dt = datetime.utcnow()

    _msg = "Culling unpacked tar balls {} days older than {}"
    if options.dry_run:
        print(_msg.format(max_unpacked_age, curr_dt.strftime(_STD_DATETIME_FMT)))
    else:
        logger.debug(_msg, max_unpacked_age, curr_dt.strftime(_STD_DATETIME_FMT))

    actions_taken = []
    errors = 0
    start = pbench.server._time()

    gen = gen_list_unpacked_aged(incomingpath, archivepath, curr_dt, max_unpacked_age)
    if config._unittests:
        # force the generator and sort the list
        gen = sorted(list(gen))

    for tb_incoming_dir, controller_name in gen:
        act_set = remove_unpacked(
            tb_incoming_dir,
            controller_name,
            resultspath,
            userspath,
            logger,
            options.dry_run,
        )
        unpacked_dir_name = Path(tb_incoming_dir).name
        act_path = Path(controller_name, unpacked_dir_name)
        act_set.set_name(act_path)
        actions_taken.append(act_set)
        if act_set.errors > 0:
            # Stop any further unpacked tar ball removal if an error is
            # encountered.
            break
    end = pbench.server._time()

    # Generate the ${TOP}/public_html prefix so we can strip it from the
    # various targets in the report.
    public_html = os.path.realpath(os.path.join(config.TOP, "public_html"))

    # Write the actions taken into a report file.
    with tempfile.NamedTemporaryFile(
        mode="w+t", prefix=f"{_NAME_}.", suffix=".report", dir=config.TMP
    ) as tfp:
        duration = end - start
        total = len(actions_taken)
        print(
            f"Culled {total:d} unpacked tar ball directories ({errors:d}"
            f" errors) in {duration:0.2f} secs",
            file=tfp,
        )
        if total > 0:
            print("\nActions Taken:", file=tfp)
        for act_set in sorted(actions_taken, key=lambda a: a.name):
            print(
                f"  - {act_set.name} ({act_set.errors:d} errors,"
                f" {act_set.duration():0.2f} secs)",
                file=tfp,
            )
            for act in act_set.actions:
                assert act.noun.startswith(
                    public_html
                ), f"Logic bomb! {act.noun} not in .../public_html/"
                tgt = Path(act.noun[len(public_html) + 1 :])
                if act.verb == "mv":
                    name = tgt.name
                    controller = tgt.parent
                    ex_tgt = controller / f".delete.{name}"
                    print(
                        f"      $ {act.verb} {tgt} {ex_tgt}  # {act.status}", file=tfp
                    )
                else:
                    print(f"      $ {act.verb} {tgt}  # {act.status}", file=tfp)

        # Flush out the report ahead of posting it.
        tfp.flush()
        tfp.seek(0)

        # We need to generate a report that lists all the actions taken.
        report = Report(config, _NAME_)
        report.init_report_template()
        try:
            report.post_status(
                config.timestamp(), "status" if errors == 0 else "errors", tfp.name
            )
        except Exception:
            pass
    return errors


if __name__ == "__main__":
    parser = ArgumentParser(f"Usage: {_NAME_} [--config <path-to-config-file>]")
    parser.add_argument("-C", "--config", dest="cfg_name", help="Specify config file")
    parser.add_argument(
        "-D",
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Perform a dry-run only",
    )
    parser.set_defaults(cfg_name=os.environ.get("_PBENCH_SERVER_CONFIG"))
    parsed = parser.parse_args()
    status = main(parsed)
    sys.exit(status)
