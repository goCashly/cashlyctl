from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except Exception:  # pragma: no cover - optional dependency at runtime
    boto3 = None  # type: ignore[assignment]
    BotoCoreError = Exception  # type: ignore[assignment]
    ClientError = Exception  # type: ignore[assignment]


@dataclass(slots=True)
class AwsInstanceDetail:
    target_name: str
    target_url: str
    region: str
    hostname: str
    identity_name: str = "-"
    instance_id: str = "-"
    state: str = "-"
    availability_zone: str = "-"
    private_ip: str = "-"
    public_ip: str = "-"
    launch_time: str = "-"
    uptime: str = "-"
    instance_type: str = "-"
    image_id: str = "-"
    os_guess: str = "-"
    alb_listener: str = "-"
    target_group: str = "-"
    target_health: str = "-"
    target_health_reason: str = "-"
    status_check_system: str = "-"
    status_check_instance: str = "-"
    cpu_avg_15m: str = "-"
    origin_ip: str = "-"
    source_note: str = ""
    error: str = ""


def aws_sdk_available() -> bool:
    return boto3 is not None


def fetch_instance_detail(target_name: str, target_url: str) -> AwsInstanceDetail:
    hostname = urlparse(target_url).hostname or "-"
    region = _resolve_region(target_name)
    detail = AwsInstanceDetail(
        target_name=target_name,
        target_url=target_url,
        region=region,
        hostname=hostname,
    )
    if boto3 is None:
        detail.error = "boto3 not installed"
        return detail

    try:
        session = boto3.Session(region_name=region)
        ec2 = session.client("ec2")
        elbv2 = session.client("elbv2")
        cloudwatch = session.client("cloudwatch")
    except Exception as exc:
        detail.error = f"aws client init failed: {exc}"
        return detail

    instance = _find_instance(ec2, target_name)
    if not instance:
        detail.error = "instance not found (check tag Name / permissions / region)"
        detail.source_note = "Set CASHLYCTL_AWS_INSTANCE_ID_<TARGET> or CASHLYCTL_AWS_INSTANCE_NAME_<TARGET>."
        return detail

    detail.identity_name = _tag_name(instance) or target_name
    detail.instance_id = str(instance.get("InstanceId", "-"))
    detail.state = str(instance.get("State", {}).get("Name", "-"))
    detail.availability_zone = str(instance.get("Placement", {}).get("AvailabilityZone", "-"))
    detail.private_ip = str(instance.get("PrivateIpAddress", "-"))
    detail.public_ip = str(instance.get("PublicIpAddress", "-"))
    detail.launch_time = _format_time(instance.get("LaunchTime"))
    detail.uptime = _format_uptime(instance.get("LaunchTime"))
    detail.instance_type = str(instance.get("InstanceType", "-"))
    detail.image_id = str(instance.get("ImageId", "-"))
    detail.os_guess = _guess_os(ec2, instance)
    detail.origin_ip = detail.public_ip if detail.public_ip != "-" else detail.private_ip

    _fill_instance_status(ec2, detail)
    _fill_cpu_metric(cloudwatch, detail)
    _fill_lb_target_health(elbv2, detail)
    return detail


def _resolve_region(target_name: str) -> str:
    key = _target_env_key(target_name)
    return (
        _runtime_env(f"CASHLYCTL_AWS_REGION_{key}", "").strip()
        or _runtime_env("CASHLYCTL_AWS_REGION", "").strip()
        or _runtime_env("AWS_REGION", "").strip()
        or _runtime_env("AWS_DEFAULT_REGION", "").strip()
        or "us-east-1"
    )


def _find_instance(ec2_client, target_name: str) -> dict[str, object] | None:
    key = _target_env_key(target_name)
    instance_id_override = _runtime_env(f"CASHLYCTL_AWS_INSTANCE_ID_{key}", "").strip()
    if instance_id_override:
        try:
            response = ec2_client.describe_instances(InstanceIds=[instance_id_override])
            instances = _flatten_instances(response)
            return instances[0] if instances else None
        except (BotoCoreError, ClientError):
            return None

    instance_name = (
        _runtime_env(f"CASHLYCTL_AWS_INSTANCE_NAME_{key}", "").strip()
        or target_name
    )
    filters = [
        {"Name": "tag:Name", "Values": [instance_name]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
    ]
    try:
        response = ec2_client.describe_instances(Filters=filters)
    except (BotoCoreError, ClientError):
        return None

    instances = _flatten_instances(response)
    if not instances:
        return None

    # Prefer running and newest launch time.
    running = [item for item in instances if str(item.get("State", {}).get("Name", "")) == "running"]
    candidates = running or instances
    candidates.sort(
        key=lambda item: item.get("LaunchTime", datetime(1970, 1, 1, tzinfo=UTC)),
        reverse=True,
    )
    return candidates[0]


def _fill_instance_status(ec2_client, detail: AwsInstanceDetail) -> None:
    if detail.instance_id == "-":
        return
    try:
        response = ec2_client.describe_instance_status(
            InstanceIds=[detail.instance_id],
            IncludeAllInstances=True,
        )
    except (BotoCoreError, ClientError):
        return
    statuses = response.get("InstanceStatuses", [])
    if not statuses:
        return
    status = statuses[0]
    detail.status_check_system = str(status.get("SystemStatus", {}).get("Status", "-"))
    detail.status_check_instance = str(status.get("InstanceStatus", {}).get("Status", "-"))


def _fill_cpu_metric(cloudwatch_client, detail: AwsInstanceDetail) -> None:
    if detail.instance_id == "-":
        return
    end_time = datetime.now(tz=UTC)
    start_time = end_time - timedelta(minutes=15)
    try:
        response = cloudwatch_client.get_metric_statistics(
            Namespace="AWS/EC2",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "InstanceId", "Value": detail.instance_id}],
            StartTime=start_time,
            EndTime=end_time,
            Period=300,
            Statistics=["Average"],
        )
    except (BotoCoreError, ClientError):
        return
    points = response.get("Datapoints", [])
    if not points:
        return
    avg = sum(float(item.get("Average", 0.0)) for item in points) / len(points)
    detail.cpu_avg_15m = f"{avg:.1f}%"


def _fill_lb_target_health(elbv2_client, detail: AwsInstanceDetail) -> None:
    if detail.instance_id == "-":
        return
    key = _target_env_key(detail.target_name)
    override_tg = _runtime_env(f"CASHLYCTL_AWS_TARGET_GROUP_{key}", "").strip()
    target_group_arns: list[str] = []
    if override_tg:
        target_group_arns.append(override_tg)
    else:
        try:
            paginator = elbv2_client.get_paginator("describe_target_groups")
            for page in paginator.paginate():
                for group in page.get("TargetGroups", []):
                    arn = str(group.get("TargetGroupArn", "")).strip()
                    if arn:
                        target_group_arns.append(arn)
        except (BotoCoreError, ClientError):
            return

    for group_arn in target_group_arns:
        try:
            health_resp = elbv2_client.describe_target_health(TargetGroupArn=group_arn)
        except (BotoCoreError, ClientError):
            continue
        descriptions = health_resp.get("TargetHealthDescriptions", [])
        match = None
        for item in descriptions:
            target = item.get("Target", {})
            if str(target.get("Id", "")) == detail.instance_id:
                match = item
                break
        if not match:
            continue

        # Found mapping for this instance.
        detail.target_group = _target_group_name(group_arn)
        target_health = match.get("TargetHealth", {})
        detail.target_health = str(target_health.get("State", "-"))
        detail.target_health_reason = str(target_health.get("Reason", "-"))
        detail.alb_listener = _listener_summary_for_target_group(elbv2_client, group_arn)
        return


def _listener_summary_for_target_group(elbv2_client, target_group_arn: str) -> str:
    try:
        tg_resp = elbv2_client.describe_target_groups(TargetGroupArns=[target_group_arn])
    except (BotoCoreError, ClientError):
        return "-"
    groups = tg_resp.get("TargetGroups", [])
    if not groups:
        return "-"
    group = groups[0]
    lb_arns = group.get("LoadBalancerArns", [])
    if not lb_arns:
        return "-"
    lb_arn = str(lb_arns[0])
    lb_name = _load_balancer_name(lb_arn)
    try:
        listener_resp = elbv2_client.describe_listeners(LoadBalancerArn=lb_arn)
    except (BotoCoreError, ClientError):
        return lb_name
    listeners = listener_resp.get("Listeners", [])
    if not listeners:
        return lb_name
    first = listeners[0]
    protocol = str(first.get("Protocol", "-"))
    port = str(first.get("Port", "-"))
    return f"{lb_name} {protocol}:{port}"


def _guess_os(ec2_client, instance: dict[str, object]) -> str:
    platform_details = str(instance.get("PlatformDetails", "")).strip()
    platform = str(instance.get("Platform", "")).strip()
    image_id = str(instance.get("ImageId", "")).strip()
    meta = f"{platform_details} {platform}".strip().lower()
    if not meta and image_id:
        try:
            images = ec2_client.describe_images(ImageIds=[image_id]).get("Images", [])
        except (BotoCoreError, ClientError):
            images = []
        if images:
            image = images[0]
            name = str(image.get("Name", ""))
            desc = str(image.get("Description", ""))
            meta = f"{name} {desc}".lower()
    if "ubuntu" in meta:
        return "Ubuntu"
    if "amazon linux" in meta or "amzn" in meta:
        return "Amazon Linux"
    if "debian" in meta:
        return "Debian"
    if "rhel" in meta or "red hat" in meta:
        return "RHEL"
    if "windows" in meta:
        return "Windows"
    if meta:
        return platform_details or platform or "Linux"
    return "Linux"


def _flatten_instances(response: dict[str, object]) -> list[dict[str, object]]:
    instances: list[dict[str, object]] = []
    for reservation in response.get("Reservations", []):
        if not isinstance(reservation, dict):
            continue
        for instance in reservation.get("Instances", []):
            if isinstance(instance, dict):
                instances.append(instance)
    return instances


def _tag_name(instance: dict[str, object]) -> str:
    for item in instance.get("Tags", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("Key", "")).lower() == "name":
            return str(item.get("Value", "")).strip()
    return ""


def _target_env_key(name: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in name).upper()


def _target_group_name(target_group_arn: str) -> str:
    if ":targetgroup/" not in target_group_arn:
        return target_group_arn
    suffix = target_group_arn.split(":targetgroup/", 1)[1]
    return suffix.split("/", 1)[0]


def _load_balancer_name(load_balancer_arn: str) -> str:
    marker = ":loadbalancer/"
    if marker not in load_balancer_arn:
        return load_balancer_arn
    suffix = load_balancer_arn.split(marker, 1)[1]
    parts = suffix.split("/")
    if len(parts) >= 2:
        return parts[1]
    return suffix


def _format_time(value: object) -> str:
    if not isinstance(value, datetime):
        return "-"
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _format_uptime(value: object) -> str:
    if not isinstance(value, datetime):
        return "-"
    now = datetime.now(tz=UTC)
    delta = now - value.astimezone(UTC)
    total = int(max(0, delta.total_seconds()))
    days = total // 86400
    hours = (total % 86400) // 3600
    mins = (total % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h {mins}m"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


_RUNTIME_ENV_CACHE: dict[str, str] | None = None


def _runtime_env(key: str, default: str) -> str:
    value = os.getenv(key, "").strip()
    if value:
        return value
    env_data = _load_runtime_env_file()
    file_value = env_data.get(key, "").strip()
    if file_value:
        return file_value
    return default


def _load_runtime_env_file(path: str = ".env") -> dict[str, str]:
    global _RUNTIME_ENV_CACHE
    if _RUNTIME_ENV_CACHE is not None:
        return _RUNTIME_ENV_CACHE

    data: dict[str, str] = {}
    env_path = Path(path)
    if not env_path.exists():
        _RUNTIME_ENV_CACHE = data
        return data

    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        _RUNTIME_ENV_CACHE = data
        return data

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        env_key, value = line.split("=", 1)
        key_clean = env_key.strip()
        value_clean = value.strip().strip('"').strip("'")
        if key_clean and value_clean:
            data[key_clean] = value_clean

    _RUNTIME_ENV_CACHE = data
    return data
