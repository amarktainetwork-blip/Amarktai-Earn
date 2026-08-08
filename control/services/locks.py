from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from control.models import Job, JobLock


class JobLockUnavailable(RuntimeError):
    pass


@transaction.atomic
def acquire_job_lock(job_id, node_id: str, lease_seconds: int = 300) -> JobLock:
    now = timezone.now()
    job = Job.objects.select_for_update().get(pk=job_id)
    lock = JobLock.objects.select_for_update().filter(job=job).first()
    lease_until = now + timedelta(seconds=lease_seconds)
    if lock is None:
        return JobLock.objects.create(job=job, node_id=node_id, lease_until=lease_until, fencing_token=1)
    if lock.lease_until > now and lock.node_id != node_id:
        raise JobLockUnavailable(f"job {job_id} is leased by {lock.node_id} until {lock.lease_until.isoformat()}")
    lock.node_id = node_id
    lock.lease_until = lease_until
    lock.fencing_token += 1
    lock.save(update_fields=["node_id", "lease_until", "fencing_token", "updated_at"])
    return lock


@transaction.atomic
def renew_job_lock(job_id, node_id: str, fencing_token: int, lease_seconds: int = 300) -> JobLock:
    lock = JobLock.objects.select_for_update().get(job_id=job_id)
    if lock.node_id != node_id or lock.fencing_token != fencing_token or lock.lease_until <= timezone.now():
        raise JobLockUnavailable("stale or expired fencing token")
    lock.lease_until = timezone.now() + timedelta(seconds=lease_seconds)
    lock.save(update_fields=["lease_until", "updated_at"])
    return lock


@transaction.atomic
def release_job_lock(job_id, node_id: str, fencing_token: int) -> None:
    lock = JobLock.objects.select_for_update().filter(job_id=job_id).first()
    if lock is None:
        return
    if lock.node_id != node_id or lock.fencing_token != fencing_token:
        raise JobLockUnavailable("cannot release a lock owned by another lease")
    lock.delete()
