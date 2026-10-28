"""
PipelineRunLogger Service
Handles automatic telemetry logging, execution metrics, validation scores, and audit particulars
into the PostgreSQL pipeline_runs table and the date-partitioned output/ audit logs.
"""

import os
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List
from sqlalchemy.orm import Session

from db.connection import get_session
from db.models.pipeline_run import PipelineRun
from serializers.account_serializer import slugify


class PipelineRunLogger:
    @staticmethod
    def log_event(
        company_name: str,
        level: Optional[str] = None,  # 'account', 'lob', 'persona', 'composite'
        action: str = "run", # 'pull', 'validate', 'dump', 'run', 'manual_edit', 'manual_verify'
        target_url: Optional[str] = None,
        status: str = "completed",  # 'staged', 'validated', 'success', 'failed'
        quality_score: float = 0.0,
        quality_grade: str = "N/A",
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        duration_seconds: float = 0.0,
        entities_extracted: Optional[Dict[str, Any]] = None,
        raw_storage_dir: Optional[str] = None,
        enriched_storage_dir: Optional[str] = None,
        execution_logs: Optional[List[Dict[str, Any]]] = None,
        error_message: Optional[str] = None,
        total_credits_used: int = 0,
        credits_breakdown: Optional[Dict[str, Any]] = None,
        session: Optional[Session] = None,
        pipeline_level: Optional[str] = None,
    ) -> Optional[PipelineRun]:
        effective_level = level or pipeline_level or "composite"
        now = datetime.now(timezone.utc)
        start_time = started_at or now
        end_time = completed_at or now
        if not duration_seconds and end_time >= start_time:
            duration = round((end_time - start_time).total_seconds(), 2)
        else:
            duration = float(duration_seconds or 0.0)

        safe_company = (company_name or "general").strip()
        comp_slug = slugify(safe_company)[:30] or "run"
        run_id = f"run_{comp_slug}_{effective_level}_{action}_{start_time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        entities_dict = dict(entities_extracted or {})
        entities_dict["level"] = effective_level
        entities_dict["action"] = action
        entities_dict["company"] = safe_company

        logs_list = list(execution_logs or [])
        if not logs_list:
            log_entry = {
                "timestamp": end_time.isoformat(),
                "level": level,
                "action": action,
                "step_name": f"Pipeline Run ({action})",
                "status": status,
                "quality_score": float(quality_score or 0.0),
                "quality_grade": quality_grade or "N/A",
                "duration_seconds": duration,
                "particulars": entities_dict,
            }
            if error_message:
                log_entry["error"] = error_message
            logs_list.append(log_entry)
        else:
            has_completion = any(l.get("action") == "completion" or str(l.get("step_name", "")).startswith("Pipeline Execution Completed") for l in logs_list)
            if not has_completion:
                completion_entry = {
                    "timestamp": end_time.isoformat(),
                    "level": level,
                    "action": "completion",
                    "step_name": f"Pipeline Execution Completed ({status.upper()})",
                    "status": status,
                    "quality_score": float(quality_score or 0.0),
                    "quality_grade": quality_grade or "N/A",
                    "duration_seconds": duration,
                    "message": f"Execution finished for {safe_company} in {duration}s with status '{status}'.",
                    "particulars": entities_dict,
                }
                logs_list.append(completion_entry)

        try:
            date_str = start_time.strftime("%Y-%m-%d")
            base_output = Path("output") / date_str
            base_output.mkdir(parents=True, exist_ok=True)
            audit_file = base_output / "pipeline_runs_audit.jsonl"
            with open(audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "run_id": run_id,
                    "company_name": safe_company,
                    "target_url": target_url,
                    "level": level,
                    "action": action,
                    "status": status,
                    "quality_score": float(quality_score or 0.0),
                    "quality_grade": quality_grade or "N/A",
                    "started_at": start_time.isoformat(),
                    "completed_at": end_time.isoformat(),
                    "duration_seconds": duration,
                    "entities_extracted": entities_dict,
                    "raw_storage_dir": str(raw_storage_dir) if raw_storage_dir else None,
                    "enriched_storage_dir": str(enriched_storage_dir) if enriched_storage_dir else None,
                    "error_message": error_message,
                }) + "\n")
        except Exception as file_err:
            print(f"[!] [PipelineRunLogger] Notice writing file audit log: {file_err}")

        close_session = False
        db_session = session
        if db_session is None:
            db_session = get_session()
            close_session = True

        try:
            run_row = PipelineRun(
                run_id=run_id,
                company_name=safe_company,
                target_url=target_url,
                status=status,
                quality_score=float(quality_score or 0.0),
                quality_grade=quality_grade or "N/A",
                started_at=start_time,
                completed_at=end_time,
                duration_seconds=duration,
                total_credits_used=int(total_credits_used or 0),
                credits_breakdown=credits_breakdown or {},
                entities_extracted=entities_dict,
                raw_storage_dir=str(raw_storage_dir) if raw_storage_dir else None,
                enriched_storage_dir=str(enriched_storage_dir) if enriched_storage_dir else None,
                execution_logs=logs_list,
                error_message=error_message,
            )
            db_session.add(run_row)
            db_session.commit()
            db_session.refresh(run_row)
            return run_row
        except Exception as db_err:
            db_session.rollback()
            print(f"[!] [PipelineRunLogger] DB persistence error: {db_err}")
            return None
        finally:
            if close_session:
                db_session.close()