from rest_framework import serializers

from .models import Job


class JobSerializer(serializers.ModelSerializer):
    instance_name = serializers.CharField(source="instance.name", read_only=True, default=None)
    user_name = serializers.CharField(source="user.username", read_only=True, default=None)

    class Meta:
        model = Job
        fields = [
            "id",
            "kind",
            "title",
            "instance",
            "instance_name",
            "user",
            "user_name",
            "status",
            "progress",
            "message",
            "error",
            "error_code",
            "result",
            "log",
            "created_at",
            "started_at",
            "finished_at",
        ]
        read_only_fields = fields
