import copy
import logging
import networkx as nx
import os
import re
import requests
import shlex
import subprocess
import yaml

from pathlib import Path
from urllib.parse import urlparse

from runner.exceptions import CohortExtractorError
from runner.exceptions import DependencyFailed
from runner.exceptions import DependencyRunning
from runner.exceptions import OpenSafelyError
from runner.exceptions import ProjectValidationError
from runner.exceptions import ScriptError
from runner.utils import getlogger
from runner.utils import get_auth
from runner.utils import safe_join


logger = getlogger(__name__)
baselogger = logging.LoggerAdapter(logger, {"job_id": "-"})

# These numbers correspond to "levels" as described in our security
# documentation
PRIVACY_LEVEL_HIGH = 3
PRIVACY_LEVEL_MEDIUM = 4

# The keys of this dictionary are all the supported `run` commands in
# jobs
RUN_COMMANDS_CONFIG = {
    "cohortextractor": {
        "input_privacy_level": None,
        "output_privacy_level": PRIVACY_LEVEL_HIGH,
        "docker_invocation": [
            "docker.opensafely.org/cohort-extractor",
            "generate_cohort",
            "--output-dir=/workspace",
        ],
        "docker_exception": CohortExtractorError,
    },
    "stata-mp": {
        "input_privacy_level": PRIVACY_LEVEL_HIGH,
        "output_privacy_level": PRIVACY_LEVEL_MEDIUM,
        "docker_invocation": ["docker.opensafely.org/stata-mp"],
        "docker_exception": ScriptError,
    },
}


def make_volume_name(repo, branch_or_tag, db_flavour):
    """Create a string suitable for naming a folder that will contain
    data, using state related to the current job as a unique key.

    """
    repo_name = urlparse(repo).path[1:]
    if repo_name.endswith("/"):
        repo_name = repo_name[:-1]
    repo_name = repo_name.split("/")[-1]
    return repo_name + "-" + branch_or_tag + "-" + db_flavour


def get_latest_matching_job_from_queue(
    repo=None, db=None, tag=None, action_id=None, **kw
):
    job = {
        "backend": os.environ["BACKEND"],
        "repo": repo,
        "db": db,
        "tag": tag,
        "operation": action_id,
        "limit": 1,
    }
    response = requests.get(
        os.environ["JOB_SERVER_ENDPOINT"], params=job, auth=get_auth()
    )
    response.raise_for_status()
    results = response.json()["results"]
    return results[0] if results else None


def push_dependency_job_from_action_to_queue(action):
    job = {
        "backend": os.environ["BACKEND"],
        "repo": action["repo"],
        "db": action["db"],
        "tag": action["tag"],
        "operation": action["action_id"],
    }
    job["callback_url"] = action["callback_url"]
    job["needed_by"] = action["needed_by"]
    response = requests.post(
        os.environ["JOB_SERVER_ENDPOINT"], json=job, auth=get_auth()
    )
    response.raise_for_status()
    return response


def docker_container_exists(container_name):
    cmd = [
        "docker",
        "ps",
        "--filter",
        f"name={container_name}",
        "--quiet",
    ]
    result = subprocess.run(cmd, capture_output=True, encoding="utf8")
    return result.stdout != ""


def start_dependent_jobs_or_raise_if_unfinished(action):
    """Does the target output file for this job exist?  If not, raise an
    exception to prevent this action from starting.

    `DependencyRunning` exceptions have special handling in the main
    loop so the job can be retried as necessary

    """
    for output_name, output_filename in action.get("outputs", {}).items():
        expected_path = os.path.join(action["output_bucket"], output_filename)
        if os.path.exists(expected_path):
            continue

        if docker_container_exists(action["container_name"]):
            raise DependencyRunning(
                f"Not started because dependency `{action['action_id']}` is currently running (as {action['container_name']})",
                report_args=True,
            )

        dependency_status = get_latest_matching_job_from_queue(**action)
        baselogger.info(
            "Got job %s from queue to match %s", dependency_status, action["action_id"],
        )
        if dependency_status:
            if dependency_status["completed_at"]:
                if dependency_status["status_code"] == 0:
                    new_job = push_dependency_job_from_action_to_queue(action)
                    raise DependencyRunning(
                        f"Not started because dependency `{action['action_id']}` has been added to the job queue at {new_job['url']} as its previous output can no longer be found",
                        report_args=True,
                    )
                else:
                    raise DependencyFailed(
                        f"Dependency `{action['action_id']}` failed, so unable to run this operation",
                        report_args=True,
                    )

            elif dependency_status["started"]:
                raise DependencyRunning(
                    f"Not started because dependency `{action['action_id']}` is just about to start",
                    report_args=True,
                )
            else:
                raise DependencyRunning(
                    f"Not started because dependency `{action['action_id']}` is waiting to start",
                    report_args=True,
                )
        # To reach this point, the job has never been run
        push_dependency_job_from_action_to_queue(action)
        raise DependencyRunning(
            f"Not started because dependency `{action['action_id']}` has been added to the job queue",
            report_args=True,
        )


def escape_braces(unescaped_string):
    """Escape braces so that they will be preserved through a string
    `format()` operation

    """
    return unescaped_string.replace("{", "{{").replace("}", "}}")


def variables_in_string(string_with_variables, variable_name_only=False):
    """Return a list of variables of the form `${{ var }}` (or `${{var}}`)
    in the given string.

    Setting the `variable_name_only` flag will a list of variables of
    the form `var`

    """
    matches = re.findall(
        r"(\$\{\{ ?([A-Za-z][A-Za-z0-9.-_]+) ?\}\})", string_with_variables
    )
    if variable_name_only:
        return [x[1] for x in matches]
    else:
        return [x[0] for x in matches]


def load_and_validate_project(workdir):
    """Check that a dictionary of project actions is valid
    """
    with open(os.path.join(workdir, "project.yaml"), "r") as f:
        project = yaml.safe_load(f)

    expected_version = project.get("version", None)
    if expected_version != "1.0":
        raise ProjectValidationError(
            f"Project file must specify a valid version (currently only 1.0)"
        )
    seen_runs = []
    project_actions = project["actions"]
    for action_id, action_config in project_actions.items():
        # Check it's a permitted run command
        name, version, args = split_and_format_run_command(action_config["run"])
        if name not in RUN_COMMANDS_CONFIG:
            raise ProjectValidationError(name)
        if not version:
            raise ProjectValidationError(
                f"{name} must have a version specified (e.g. {name}:0.5.2)"
            )
        for output_id, filename in action_config["outputs"].items():
            try:
                safe_join(workdir, filename)
            except AssertionError:
                raise ProjectValidationError(f"Output path {filename} is not permitted")
        # Check the run command + args signature appears only once in
        # a project
        run_signature = f"{name}_{args}"
        if run_signature in seen_runs:
            raise ProjectValidationError(name, args, report_args=True)
        seen_runs.append(run_signature)

        # Check any variables are supported
        for v in variables_in_string(action_config["run"]):
            if not v.replace(" ", "").startswith("${{needs"):
                raise ProjectValidationError(
                    f"Unsupported variable {v}", report_args=True
                )
            try:
                _, action_id, outputs_key, output_id = v.split(".")
                if outputs_key != "outputs":
                    raise ProjectValidationError(
                        f"Unable to find variable {v}", report_args=True
                    )
            except ValueError:
                raise ProjectValidationError(
                    f"Unable to find variable {v}", report_args=True
                )
    return project


def interpolate_variables(args, dependency_actions):
    """Given a list of arguments, each a single string token, replace any
    that are variables using a dotted lookup against the supplied
    dependencies dictionary

    """
    interpolated_args = []
    for arg in args:
        variables = variables_in_string(arg, variable_name_only=True)
        if variables:
            try:
                # at this point, the command string has been
                # shell-split into separate tokens, so there is only
                # ever a single variable to interpolate
                _, action_id, variable_kind, variable_id = variables[0].split(".")
                dependency_action = dependency_actions[action_id]
                dependency_outputs = dependency_action[variable_kind]
                filename = dependency_outputs[variable_id]
                if variable_kind == "outputs":
                    # When copying outputs into the workspace, we
                    # namespace them by action_id, to avoid filename
                    # clashes
                    arg = f"{action_id}_{filename}"
                else:
                    arg = filename
            except (KeyError, ValueError):
                raise ProjectValidationError(
                    f"No output corresponding to {arg} was found", report_args=True
                )
        interpolated_args.append(arg)
    return interpolated_args


def split_and_format_run_command(run_command):
    """A `run` command is in the form of `run_token:optional_version [args]`.

    Shell-split this into its constituent parts, with any substitution
    tokens normalized and escaped for later parsing and formatting.

    """
    for v in variables_in_string(run_command):
        # Remove spaces to prevent shell escaping from thinking these
        # are different tokens
        run_command = run_command.replace(v, v.replace(" ", ""))
        # Escape braces to prevent python `format()` from coverting
        # doubled braces in single ones
        run_command = escape_braces(run_command)

    parts = shlex.split(run_command)
    # Commands are in the form command:version
    if ":" in parts[0]:
        run_token, version = parts[0].split(":")
    else:
        run_token = parts[0]
        version = None

    return run_token, version, parts[1:]


def add_runtime_metadata(
    action, repo=None, db=None, tag=None, callback_url=None, operation=None, **kwargs,
):
    """Given a run command specified in project.yaml, validate that it is
    permitted, and return how it should be invoked for `docker run`

    Adds docker_invocation, docker_exception, privacy_level,
    database_url, container_name, and output_bucket to the `action` dict.

    """
    action = copy.deepcopy(action)
    command = action["run"]
    name, version, args = split_and_format_run_command(command)

    # Convert human-readable database name into DATABASE_URL
    action["database_url"] = os.environ[f"{db.upper()}_DATABASE_URL"]
    info = copy.deepcopy(RUN_COMMANDS_CONFIG[name])

    # Convert the command name into a full set of arguments that can
    # be passed to `docker run`, but preserving user-defined variables
    # in the form `${{ variable }}` for interpolation later (after the
    # dependences have been walked)
    docker_invocation = info["docker_invocation"]
    if version:
        docker_invocation[0] = docker_invocation[0] + ":" + version

    action["output_bucket"] = make_path(
        repo=repo, tag=tag, db=db, privacy_level=info["output_privacy_level"]
    )
    action["container_name"] = make_container_name(action["output_bucket"])
    action["docker_exception"] = info["docker_exception"]

    # Interpolate action dictionary into argument template
    docker_invocation = docker_invocation + args

    action["docker_invocation"] = [arg.format(**action) for arg in docker_invocation]
    action["callback_url"] = callback_url
    action["repo"] = repo
    action["db"] = db
    action["tag"] = tag
    action["needed_by"] = operation
    return action


def get_namespaced_outputs_from_dependencies(dependency_actions):
    """Given a list of dependencies, construct a dictionary that
    represents all the files these dependencies are expected to
    output. To avoid filename clashes, these are namespaced by the
    action that outputs the file.

    """
    inputs = {}
    for dependency in dependency_actions.values():
        for output_file in dependency["outputs"].values():
            # Namespace the output file with the action id, so that
            # copying files *into* the workspace doesn't overwrite
            # anything
            inputs[f"{dependency['action_id']}_{output_file}"] = os.path.join(
                dependency["output_bucket"], output_file
            )
    return inputs


def parse_project_yaml(workdir, job_spec):
    """Given a checkout of an OpenSAFELY repo containing a `project.yml`,
    check the provided job can run, and if so, update it with
    information about how to run it in a docker container.

    If the job has unfinished dependencies, a DependencyNotFinished
    exception is raised.

    """
    project = load_and_validate_project(workdir)
    project_actions = project["actions"]
    requested_action_id = job_spec["operation"]
    if requested_action_id not in project_actions:
        raise ProjectValidationError(requested_action_id)
    job_config = job_spec.copy()
    # Build dependency graph
    graph = nx.DiGraph()
    for action_id, action_config in project_actions.items():
        project_actions[action_id]["action_id"] = action_id
        graph.add_node(action_id)
        for dependency_id in action_config.get("needs", []):
            graph.add_node(dependency_id)
            graph.add_edge(dependency_id, action_id)
    dependencies = graph.predecessors(requested_action_id)

    # Compute runtime metadata for the job we're interested
    job_action = add_runtime_metadata(
        project_actions[requested_action_id], **job_config
    )

    # Do the same thing for dependencies, and also assert that they've
    # completed by checking their expected output exists
    dependency_actions = {}
    for action_id in dependencies:
        # Adds docker_invocation, docker_exception, privacy_level, and
        # output_bucket to the config
        action = add_runtime_metadata(project_actions[action_id], **job_config)
        start_dependent_jobs_or_raise_if_unfinished(action)
        dependency_actions[action_id] = action

    # Now interpolate user-provided variables into docker
    # invocation. This must happen after metadata has been added to
    # the dependencies, as variables can reference the ouputs of other
    # actions
    job_action["docker_invocation"] = interpolate_variables(
        job_action["docker_invocation"], dependency_actions
    )
    job_action["namespaced_inputs"] = get_namespaced_outputs_from_dependencies(
        dependency_actions
    )
    job_config.update(job_action)
    return job_config


def make_path(repo=None, tag=None, db=None, privacy_level=None):
    """Make a path in a location appropriate to the privacy level,
    using state (as represented by the other keyword args) as a unique
    key

    """
    volume_name = make_volume_name(repo, tag, db)
    if privacy_level == PRIVACY_LEVEL_HIGH:
        storage_base = Path(os.environ["HIGH_PRIVACY_STORAGE_BASE"])
    elif privacy_level == PRIVACY_LEVEL_MEDIUM:
        storage_base = Path(os.environ["MEDIUM_PRIVACY_STORAGE_BASE"])
    else:
        raise OpenSafelyError("Unsupported privacy level")
    output_bucket = storage_base / volume_name
    output_bucket.mkdir(parents=True, exist_ok=True)
    return str(output_bucket)


def make_container_name(volume_name):
    # By basing the container name to the volume_name, we are
    # guaranteeing only one identical job can run at once by docker
    container_name = re.sub(r"[^a-zA-Z0-9]", "-", volume_name)
    # Remove any leading dashes, as docker requires images begin with [:alnum:]
    if container_name.startswith("-"):
        container_name = container_name[1:]
    return container_name
