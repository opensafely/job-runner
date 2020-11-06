"""
This module contains the logic for starting jobs in Docker containers and
dealing with them when they are finished.

It's important that the `start_job` and `finalise_job` functions are
idempotent. This means that the job-runner can be killed at any point and will
still end up in a consistent state when it's restarted.
"""
import datetime
import json
import shlex
import shutil
import tempfile

from . import config
from . import docker
from .database import find_where
from .git import checkout_commit
from .models import SavedJobRequest
from .project import is_generate_cohort_command
from .string_utils import slugify


# We use a file with this name to mark output directories as containing the
# results of successful runs.  For debugging purposes we want to store the
# results even of failed runs, but we don't want to ever use them as the inputs
# to subsequent actions
SUCCESS_MARKER_FILE = ".success"


class JobError(Exception):
    pass


def start_job(job):
    # If we started the job but were killed before we updated the state then
    # there's nothing further to do
    if job_still_running(job):
        return
    volume = create_and_populate_volume(job)
    action_args = shlex.split(job.run_command)
    allow_network_access = False
    env = {}
    if not config.USING_DUMMY_DATA_BACKEND:
        if is_generate_cohort_command(action_args):
            allow_network_access = True
            env["DATABASE_URL"] = config.DATABASE_URLS[job.database_name]
            if config.TEMP_DATABASE_NAME:
                env["TEMP_DATABASE_NAME"] = config.TEMP_DATABASE_NAME
    # Prepend registry name
    action_args[0] = f"{config.DOCKER_REGISTRY}/{action_args[0]}"
    docker.run(
        container_name(job),
        action_args,
        volume=(volume, "/workspace"),
        env=env,
        allow_network_access=allow_network_access,
    )


def create_and_populate_volume(job):
    volume = volume_name(job)
    docker.create_volume(volume)
    # git-archive will create a tarball on stdout and docker cp will accept a
    # tarball on stdin, so if we wanted to we could do this all without a
    # temporary directory, but not worth it at this stage
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=config.TMP_DIR) as tmpdir:
        checkout_commit(job.repo_url, job.commit, tmpdir)
        docker.copy_to_volume(volume, tmpdir)
    # Copy in files from dependencies
    for action in job.requires_outputs_from:
        action_dir = high_privacy_output_dir(job, action=action)
        if not action_dir.joinpath(SUCCESS_MARKER_FILE).exists():
            raise JobError("Unexpected missing output for '{action}'")
        docker.copy_to_volume(volume, action_dir / "outputs")
    return volume


def finalise_job(job):
    output_dir = high_privacy_output_dir(job)
    tmp_output_dir = output_dir.with_suffix(".tmp")
    error = None
    try:
        save_job_outputs(job, tmp_output_dir)
    except JobError as e:
        error = e
    save_job_metadata(job, tmp_output_dir, error)
    copy_log_data_to_log_dir(job, tmp_output_dir)
    copy_medium_privacy_data(job, tmp_output_dir)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    tmp_output_dir.rename(output_dir)
    if error:
        raise error


def cleanup_job(job):
    docker.delete_container(container_name(job))
    docker.delete_volume(volume_name(job))


def save_job_outputs(job, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    container = container_name(job)
    volume = volume_name(job)
    # Dump container metadata
    container_metadata = docker.container_inspect(container, none_if_not_exists=True)
    if not container_metadata:
        raise JobError("Job container has vanished")
    redact_environment_variables(container_metadata)
    with open(output_dir / "docker_metadata.json", "w") as f:
        json.dump(container_metadata, f, indent=2)
    # Dump Docker logs
    docker.write_logs_to_file(container, output_dir / "logs.txt")
    # Extract specified outputs
    patterns = get_glob_patterns_from_spec(job.output_spec)
    all_matches = docker.glob_volume_files(volume, patterns)
    unmatched_patterns = []
    for pattern in patterns:
        files = all_matches[pattern]
        if not files:
            unmatched_patterns.append(pattern)
        for filename in files:
            dest_filename = output_dir / "outputs" / filename
            dest_filename.parent.mkdir(parents=True, exist_ok=True)
            # Only copy filles we haven't copied already: this means that if we
            # get interrupted while copying out several large files we don't
            # need to start again from scratch when we resume
            tmp_filename = dest_filename.with_suffix(".partial.tmp")
            if not dest_filename.exists():
                docker.copy_from_volume(volume, filename, tmp_filename)
                tmp_filename.rename(dest_filename)
    # Raise errors if appropriate
    if container_metadata["State"]["ExitCode"] != 0:
        raise JobError("Job exited with an error code")
    if unmatched_patterns:
        unmatched_pattern_str = ", ".join(f"'{p}'" for p in unmatched_patterns)
        raise JobError(f"No outputs found matching {unmatched_pattern_str}")


def save_job_metadata(job, output_dir, error):
    job_metadata = job.asdict()
    job_request = find_where(SavedJobRequest, id=job.job_request_id)[0]
    job_metadata["job_request"] = job_request.original
    if error:
        job_metadata["status"] = "FAILED"
        job_metadata["error_message"] = f"{type(error).__name__}: {error}"
    else:
        job_metadata["status"] = "COMPLETED"
        # Create a marker file which we can use to easily determine if this
        # directory contains the outputs of a successful job which we can then
        # use elsewhere
        output_dir.joinpath(SUCCESS_MARKER_FILE).touch()
    with open(output_dir / "job_metadata.json", "w") as f:
        json.dump(job_metadata, f, indent=2)


# Environment variables whose values do not need to be hidden from the debug
# logs. At present the only sensitive value is DATABASE_URL, but its better to
# have an explicit safelist here.
SAFE_ENVIRONMENT_VARIABLES = set(
    """
    PATH PYTHON_VERSION DEBIAN_FRONTEND DEBCONF_NONINTERACTIVE_SEEN UBUNTU_VERSION
    PYENV_SHELL PYENV_VERSION PYTHONUNBUFFERED
    """.split()
)


def redact_environment_variables(container_metadata):
    env_vars = [line.split("=", 1) for line in container_metadata["Config"]["Env"]]
    redacted_vars = [
        f"{key}=xxxx-REDACTED-xxxx"
        if key not in SAFE_ENVIRONMENT_VARIABLES
        else f"{key}={value}"
        for (key, value) in env_vars
    ]
    container_metadata["Config"]["Env"] = redacted_vars


def copy_log_data_to_log_dir(job, data_dir):
    month_dir = datetime.date.today().strftime("%Y-%m")
    log_dir = config.JOB_LOG_DIR / month_dir / container_name(job)
    log_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("docker_metadata.json", "job_metadata.json", "logs.txt"):
        copy_file(data_dir / filename, log_dir / filename)


def copy_medium_privacy_data(job, source_dir):
    output_dir = medium_privacy_output_dir(job)
    dest_dir = output_dir.with_suffix(".tmp")
    files_to_copy = {source_dir / "job_metadata.json", source_dir / "logs.txt"}
    patterns = get_glob_patterns_from_spec(job.output_spec, "moderately_sensitive")
    for pattern in patterns:
        files_to_copy.update(source_dir.joinpath("outputs").glob(pattern))
    for source_file in files_to_copy:
        if source_file.is_dir():
            continue
        relative_path = source_file.relative_to(source_dir)
        dest_file = dest_dir / relative_path
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        copy_file(source_file, dest_file)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    dest_dir.rename(output_dir)


def copy_file(source, dest):
    # shutil.copy() should be reasonably efficient in Python 3.8+, but if we
    # need to stick with 3.7 for some reason we could replace this with a
    # shellout to `cp`. See:
    # https://docs.python.org/3/library/shutil.html#shutil-platform-dependent-efficient-copy-operations
    shutil.copy(source, dest)


def get_glob_patterns_from_spec(output_spec, privacy_level=None):
    assert privacy_level in [None, "highly_sensitive", "moderately_sensitive"]
    if privacy_level is None:
        # Return all patterns across all privacy levels
        return set().union(*[i.values() for i in output_spec.values()])
    else:
        return output_spec.get(privacy_level, {}).values()


def job_still_running(job):
    return docker.container_is_running(container_name(job))


def container_name(job):
    return f"job-{job_slug(job)}"


def volume_name(job):
    return f"volume-{job_slug(job)}"


def job_slug(job):
    return slugify(f"{job.workspace}-{job.action}-{job.id}")


def high_privacy_output_dir(job, action=None):
    workspace_dir = config.HIGH_PRIVACY_WORKSPACES_DIR / job.workspace
    if action is None:
        action = job.action
    return workspace_dir / action


def medium_privacy_output_dir(job, action=None):
    workspace_dir = config.MEDIUM_PRIVACY_WORKSPACES_DIR / job.workspace
    if action is None:
        action = job.action
    return workspace_dir / action


def outputs_exist(job_request, action):
    output_dir = high_privacy_output_dir(job_request, action)
    return output_dir.joinpath(SUCCESS_MARKER_FILE).exists()