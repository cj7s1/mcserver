from __future__ import annotations

from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.errors import ApiError
from apps.core.permissions import visible_instances

from .models import Job
from .serializers import JobSerializer


def visible_jobs(user):  # type: ignore[no-untyped-def]
    qs = Job.objects.select_related("instance", "user")
    if "instance.view" in user.global_permissions():
        return qs
    return qs.filter(Q(instance__in=visible_instances(user)) | Q(user=user))


class JobListView(APIView):
    @extend_schema(responses={200: JobSerializer(many=True)})
    def get(self, request: Request) -> Response:
        qs = visible_jobs(request.user)
        if inst := request.query_params.get("instance"):
            qs = qs.filter(instance_id=inst)
        if st := request.query_params.get("status"):
            qs = qs.filter(status__in=st.split(","))
        try:
            limit = max(1, min(int(request.query_params.get("limit", "50")), 200))
        except ValueError:
            limit = 50
        return Response(JobSerializer(qs[:limit], many=True).data)


class JobDetailView(APIView):
    @extend_schema(responses={200: JobSerializer})
    def get(self, request: Request, pk: str) -> Response:
        job = visible_jobs(request.user).filter(pk=pk).first()
        if job is None:
            raise ApiError("Job not found", code="not_found", status_code=404)
        return Response(JobSerializer(job).data)


class JobAbandonView(APIView):
    """Mark a stuck queued/running job as failed so the server's operation lock is released.

    This does not stop work the agent may still be doing; check the server afterwards.
    """

    @extend_schema(request=None, responses={200: JobSerializer})
    def post(self, request: Request, pk: str) -> Response:
        from apps.audit import service as audit
        from apps.core.permissions import require

        from .models import ACTIVE_STATUSES, JobStatus
        from .runner import finish

        job = visible_jobs(request.user).filter(pk=pk).first()
        if job is None:
            raise ApiError("Job not found", code="not_found", status_code=404)
        if job.instance_id:
            require(request.user, "instance.control", job.instance)
        else:
            require(request.user, "system.settings")
        if job.status not in ACTIVE_STATUSES:
            raise ApiError("Only queued or running jobs can be abandoned", code="not_active", status_code=409)
        finish(job, JobStatus.FAILED, error=f"Marked as failed by {request.user.username}", error_code="abandoned")
        audit.record("job.abandoned", request=request, instance=job.instance, target_type="job", target_id=str(job.id),
                     details={"kind": job.kind})
        return Response(JobSerializer(job).data)
