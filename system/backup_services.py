from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core import management
from django.utils import timezone

from .models import Backup
from .services import ServiceResult, next_document_no


def backup_daily(backup_dir: str | Path | None = None, include_media: bool = True) -> ServiceResult:
    backup_root = Path(backup_dir or getattr(settings, "ERP_BACKUP_DIR", settings.BASE_DIR / "backups"))
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = timezone.localtime().strftime("%Y%m%d_%H%M%S")
    backup_no = next_document_no("BAK")
    work_dir = backup_root / f"{backup_no}_{timestamp}"
    work_dir.mkdir(parents=True, exist_ok=True)
    archive_path = backup_root / f"{backup_no}_{timestamp}.zip"

    try:
        db_dump_path = work_dir / "database.json"
        with db_dump_path.open("w", encoding="utf-8") as dump_file:
            management.call_command(
                "dumpdata",
                "--natural-foreign",
                "--natural-primary",
                "--indent",
                "2",
                stdout=dump_file,
            )

        media_root = Path(settings.MEDIA_ROOT)
        if include_media and media_root.exists():
            media_target = work_dir / "attachments"
            shutil.copytree(media_root, media_target, dirs_exist_ok=True)

        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file_path in sorted(path for path in work_dir.rglob("*") if path.is_file()):
                archive.write(file_path, file_path.relative_to(work_dir))

        checksum = calculate_sha256(archive_path)
        backup = Backup.objects.create(
            backup_no=backup_no,
            backup_type="daily",
            file_path=str(archive_path),
            file_size=archive_path.stat().st_size,
            checksum_sha256=checksum,
            status=Backup.BackupStatus.SUCCESS,
        )
        return ServiceResult(
            True,
            message="每日备份已完成",
            data={
                "backup_id": backup.id,
                "backup_no": backup.backup_no,
                "file_path": backup.file_path,
                "file_size": backup.file_size,
                "checksum_sha256": backup.checksum_sha256,
            },
        )
    except Exception as exc:
        Backup.objects.create(
            backup_no=backup_no,
            backup_type="daily",
            file_path=str(archive_path),
            file_size=archive_path.stat().st_size if archive_path.exists() else 0,
            checksum_sha256=calculate_sha256(archive_path) if archive_path.exists() else "",
            status=Backup.BackupStatus.FAILED,
        )
        return ServiceResult(False, "SYSTEM_BACKUP_FAILED", f"每日备份失败：{exc}")
    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir)


def verify_backups(limit: int = 30) -> ServiceResult:
    backups = Backup.objects.filter(status=Backup.BackupStatus.SUCCESS).order_by("-created_at")[:limit]
    checked = 0
    failed = []
    for backup in backups:
        checked += 1
        file_path = Path(backup.file_path)
        if not file_path.exists():
            failed.append({"backup_no": backup.backup_no, "error": "备份文件不存在"})
            continue
        try:
            if calculate_sha256(file_path) != backup.checksum_sha256:
                failed.append({"backup_no": backup.backup_no, "error": "校验值不一致"})
                continue
            with zipfile.ZipFile(file_path, "r") as archive:
                corrupt_member = archive.testzip()
                if corrupt_member:
                    failed.append({"backup_no": backup.backup_no, "error": f"压缩包损坏：{corrupt_member}"})
        except Exception as exc:
            failed.append({"backup_no": backup.backup_no, "error": str(exc)})

    if failed:
        return ServiceResult(
            False,
            "SYSTEM_BACKUP_VERIFY_FAILED",
            "备份校验发现异常",
            data={"checked": checked, "failed": failed},
        )
    return ServiceResult(True, message="备份校验通过", data={"checked": checked, "failed": []})


def restore_drill(backup_id: int | None = None, backup_no: str = "", extract_dir: str | Path | None = None) -> ServiceResult:
    backup = _get_backup_for_drill(backup_id=backup_id, backup_no=backup_no)
    if backup is None:
        return ServiceResult(False, "DOC_NOT_FOUND", "未找到可演练的成功备份")

    archive_path = Path(backup.file_path)
    if not archive_path.exists():
        return ServiceResult(False, "SYSTEM_BACKUP_VERIFY_FAILED", "备份文件不存在", data={"backup_no": backup.backup_no})
    try:
        actual_checksum = calculate_sha256(archive_path)
        if actual_checksum != backup.checksum_sha256:
            return ServiceResult(
                False,
                "SYSTEM_BACKUP_VERIFY_FAILED",
                "备份文件校验值不一致",
                data={"backup_no": backup.backup_no, "expected": backup.checksum_sha256, "actual": actual_checksum},
            )

        drill_root = Path(extract_dir) if extract_dir else archive_path.parent / f"{archive_path.stem}_restore_drill"
        if drill_root.exists():
            shutil.rmtree(drill_root)
        drill_root.mkdir(parents=True, exist_ok=True)

        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                corrupt_member = archive.testzip()
                if corrupt_member:
                    return ServiceResult(
                        False,
                        "SYSTEM_BACKUP_VERIFY_FAILED",
                        "备份压缩包损坏",
                        data={"backup_no": backup.backup_no, "corrupt_member": corrupt_member},
                    )
                unsafe_member = _safe_extract_zip(archive, drill_root)
                if unsafe_member:
                    return ServiceResult(
                        False,
                        "SYSTEM_BACKUP_VERIFY_FAILED",
                        "备份压缩包包含不安全路径",
                        data={"backup_no": backup.backup_no, "unsafe_member": unsafe_member},
                    )

            db_dump_path = drill_root / "database.json"
            if not db_dump_path.exists():
                return ServiceResult(
                    False,
                    "SYSTEM_BACKUP_VERIFY_FAILED",
                    "备份包缺少 database.json",
                    data={"backup_no": backup.backup_no},
                )
            with db_dump_path.open("r", encoding="utf-8") as dump_file:
                objects = json.load(dump_file)
            if not isinstance(objects, list):
                return ServiceResult(
                    False,
                    "SYSTEM_BACKUP_VERIFY_FAILED",
                    "database.json 格式不是 Django dumpdata 列表",
                    data={"backup_no": backup.backup_no},
                )

            invalid_object_count = 0
            for item in objects:
                if not isinstance(item, dict) or "model" not in item or "fields" not in item:
                    invalid_object_count += 1
            if invalid_object_count:
                return ServiceResult(
                    False,
                    "SYSTEM_BACKUP_VERIFY_FAILED",
                    "database.json 存在无法识别的数据对象",
                    data={"backup_no": backup.backup_no, "invalid_object_count": invalid_object_count},
                )

            attachments_dir = drill_root / "attachments"
            attachment_file_count = (
                len([path for path in attachments_dir.rglob("*") if path.is_file()]) if attachments_dir.exists() else 0
            )
            return ServiceResult(
                True,
                message="备份恢复演练校验通过",
                data={
                    "backup_id": backup.id,
                    "backup_no": backup.backup_no,
                    "object_count": len(objects),
                    "attachment_file_count": attachment_file_count,
                    "extract_dir": str(drill_root),
                },
            )
        finally:
            if extract_dir is None and drill_root.exists():
                shutil.rmtree(drill_root)
    except Exception as exc:
        return ServiceResult(False, "SYSTEM_BACKUP_RESTORE_DRILL_FAILED", f"备份恢复演练失败：{exc}")


def cleanup_backups(
    keep_daily_days: int = 30,
    keep_weekly: int = 12,
    keep_monthly: int = 12,
    keep_failed_days: int = 30,
    backup_dir: str | Path | None = None,
) -> ServiceResult:
    backup_root = _backup_root(backup_dir)
    now = timezone.now()
    cutoff_daily = now - timedelta(days=keep_daily_days)
    keep_ids = set(
        Backup.objects.filter(status=Backup.BackupStatus.SUCCESS, created_at__gte=cutoff_daily).values_list("id", flat=True)
    )

    older_successful = Backup.objects.filter(
        status=Backup.BackupStatus.SUCCESS,
        created_at__lt=cutoff_daily,
    ).order_by("-created_at")

    weekly_keys = set()
    monthly_keys = set()
    for backup in older_successful:
        local_created = timezone.localtime(backup.created_at)
        week_key = local_created.isocalendar()[:2]
        month_key = (local_created.year, local_created.month)
        if week_key not in weekly_keys and len(weekly_keys) < keep_weekly:
            keep_ids.add(backup.id)
            weekly_keys.add(week_key)
        if backup.id not in keep_ids and month_key not in monthly_keys and len(monthly_keys) < keep_monthly:
            keep_ids.add(backup.id)
            monthly_keys.add(month_key)

    failed_cutoff = now - timedelta(days=keep_failed_days)
    delete_queryset = Backup.objects.exclude(id__in=keep_ids).filter(
        status=Backup.BackupStatus.SUCCESS
    ) | Backup.objects.filter(status=Backup.BackupStatus.FAILED, created_at__lt=failed_cutoff)

    deleted_records = 0
    deleted_files = 0
    file_errors = []
    for backup in delete_queryset.order_by("created_at"):
        file_path = _safe_backup_file_path(backup.file_path, backup_root)
        if file_path is None:
            file_errors.append({"backup_no": backup.backup_no, "error": "备份文件路径不在备份目录内"})
            continue
        if file_path.exists():
            try:
                file_path.unlink()
                deleted_files += 1
            except Exception as exc:
                file_errors.append({"backup_no": backup.backup_no, "error": str(exc)})
                continue
        backup.delete()
        deleted_records += 1

    if file_errors:
        return ServiceResult(
            False,
            "SYSTEM_BACKUP_CLEANUP_PARTIAL_FAILED",
            "部分旧备份清理失败",
            data={
                "deleted_records": deleted_records,
                "deleted_files": deleted_files,
                "file_errors": file_errors,
                "kept_records": len(keep_ids),
            },
        )
    return ServiceResult(
        True,
        message="旧备份清理完成",
        data={
            "deleted_records": deleted_records,
            "deleted_files": deleted_files,
            "kept_records": len(keep_ids),
            "weekly_kept": len(weekly_keys),
            "monthly_kept": len(monthly_keys),
        },
    )


def calculate_sha256(file_path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(file_path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_root(backup_dir: str | Path | None = None) -> Path:
    return Path(backup_dir or getattr(settings, "ERP_BACKUP_DIR", settings.BASE_DIR / "backups")).resolve(strict=False)


def _safe_backup_file_path(file_path: str, backup_root: Path) -> Path | None:
    if not file_path:
        return None
    try:
        candidate = Path(file_path)
        if not candidate.is_absolute():
            candidate = backup_root / candidate
        resolved_path = candidate.resolve(strict=False)
        resolved_path.relative_to(backup_root)
    except (OSError, ValueError):
        return None
    return resolved_path


def _safe_extract_zip(archive: zipfile.ZipFile, target_dir: Path) -> str:
    target_root = target_dir.resolve()
    for member in archive.infolist():
        member_path = Path(member.filename)
        if member_path.is_absolute() or member_path.drive or ".." in member_path.parts:
            return member.filename
        resolved_target = (target_root / member_path).resolve(strict=False)
        try:
            resolved_target.relative_to(target_root)
        except ValueError:
            return member.filename
        if member.is_dir():
            resolved_target.mkdir(parents=True, exist_ok=True)
            continue
        resolved_target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member, "r") as source, resolved_target.open("wb") as destination:
            shutil.copyfileobj(source, destination)
    return ""


def _get_backup_for_drill(backup_id: int | None = None, backup_no: str = "") -> Backup | None:
    queryset = Backup.objects.filter(status=Backup.BackupStatus.SUCCESS)
    if backup_id is not None:
        return queryset.filter(id=backup_id).first()
    if backup_no:
        return queryset.filter(backup_no=backup_no).first()
    return queryset.order_by("-created_at").first()
