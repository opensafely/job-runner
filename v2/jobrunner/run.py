import datetime
import time

from . import config
from .database import find_where, count_where, update, select_values
from .models import Job, State
from .manage_jobs import (
    JobError,
    start_job,
    job_still_running,
    finalise_job,
    cleanup_job,
    job_slug,
)


def main(exit_when_done=False):
    while True:
        job_count = handle_jobs()
        if exit_when_done and job_count == 0:
            break
        time.sleep(config.JOB_LOOP_INTERVAL)


def handle_jobs():
    jobs = find_where(Job, status__in=[State.PENDING, State.RUNNING])
    for job in jobs:
        if job.status == State.PENDING:
            handle_pending_job(job)
        elif job.status == State.RUNNING:
            handle_running_job(job)
    return len(jobs)


def handle_pending_job(job):
    awaited_states = get_states_of_awaited_jobs(job)
    if State.FAILED in awaited_states:
        mark_job_as_failed(job, JobError("Not starting as dependency failed"))
    elif all(state == State.COMPLETED for state in awaited_states):
        if not job_running_capacity_available():
            log(job, "Waiting for available workers", timestamp=True)
        else:
            try:
                log(job, "Starting")
                start_job(job)
            except JobError as e:
                mark_job_as_failed(job, e)
                cleanup_job(job)
            else:
                mark_job_as_running(job)
    else:
        log(job, "Waiting on dependencies", timestamp=True)


def handle_running_job(job):
    if job_still_running(job):
        log(job, "Running", timestamp=True)
    else:
        try:
            log(job, "Finished, copying outputs")
            finalise_job(job)
        except JobError as e:
            mark_job_as_failed(job, e)
        else:
            mark_job_as_completed(job)
        finally:
            cleanup_job(job)


def get_states_of_awaited_jobs(job):
    job_ids = job.wait_for_job_ids
    if not job_ids:
        return []
    return select_values(Job, "status", id__in=job_ids)


def mark_job_as_failed(job, exception):
    job.status = State.FAILED
    job.status_message = f"{type(exception).__name__}: {exception}"
    update(job, update_fields=["status", "status_message"])
    display(job)


def mark_job_as_running(job):
    job.status = State.RUNNING
    job.status_message = "Started"
    update(job, update_fields=["status", "status_message"])
    display(job)


def mark_job_as_completed(job):
    job.status = State.COMPLETED
    job.status_message = "Completed successfully"
    update(job, update_fields=["status", "status_message"])
    display(job)


def job_running_capacity_available():
    running_jobs = count_where(Job, status=State.RUNNING)
    return running_jobs < config.MAX_WORKERS


def log(job, message, timestamp=False):
    # A bit of a hack, but hopefully worthwhile: there are certain states
    # (waiting and running) which we can expect some jobs to stay in a for a
    # long time. We don't want to spam the logs (or the database) by writing
    # these out everytime. But if we only write them when they change then we
    # may have to look a long way back in the logs to find the last update, and
    # it's harder for users to have confidence that the job really is still
    # running or waiting. By appending a timestamp and ignoring the final
    # character when checking for changes we end up only logging the state
    # every 10 minutes which seems about right.
    if timestamp:
        now = datetime.datetime.now(datetime.timezone.utc)
        message = f"{message} at {now:%Y-%m-%d %H:%M}"
        if job.status_message:
            changed = job.status_message[:-1] != message[:-1]
        else:
            changed = True
    else:
        changed = job.status_message != message
    if changed:
        job.status_message = message
        update(job, update_fields=["status_message"])
        display(job)


def display(job):
    print(f"Job #{job_slug(job)}: {job.status_message}")


if __name__ == "__main__":
    main()
