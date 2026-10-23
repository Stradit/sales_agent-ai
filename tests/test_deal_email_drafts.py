"""Tests for deal email drafts persistence and history API."""
import pytest
from sqlalchemy import text
from db.connection import get_session


def test_deal_email_drafts_table_exists():
    with get_session() as s:
        # Check table columns exist
        cols = s.execute(text("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'deal_email_drafts'
            ORDER BY ordinal_position
        """)).fetchall()
        col_names = [c[0] for c in cols]
        assert "id" in col_names
        assert "deal_id" in col_names
        assert "user_id" in col_names
        assert "subject" in col_names
        assert "body" in col_names
        assert "recipient_name" in col_names
        assert "recipient_email" in col_names
        assert "created_at" in col_names


def test_deal_email_draft_insert_and_fetch():
    with get_session() as s:
        deal = s.execute(text("SELECT id FROM deals LIMIT 1")).fetchone()
        if not deal:
            pytest.skip("No deals in database")
        deal_id = deal[0]

        # Insert test draft
        res = s.execute(text("""
            INSERT INTO deal_email_drafts (deal_id, subject, body, recipient_name, recipient_email)
            VALUES (:deal_id, :subj, :body, :rname, :remail)
            RETURNING id, created_at
        """), {
            "deal_id": deal_id,
            "subj": "Test Subject for Discovery",
            "body": "Test email body content",
            "rname": "Test Recipient",
            "remail": "test@example.com"
        }).fetchone()
        draft_id = res[0]
        s.commit()

        # Query history
        row = s.execute(text("""
            SELECT id, deal_id, subject, body, recipient_name, recipient_email, created_at
            FROM deal_email_drafts
            WHERE id = :id
        """), {"id": draft_id}).mappings().fetchone()

        assert row is not None
        assert row["deal_id"] == deal_id
        assert row["subject"] == "Test Subject for Discovery"
        assert row["body"] == "Test email body content"
        assert row["recipient_name"] == "Test Recipient"
        assert row["recipient_email"] == "test@example.com"

        # Cleanup test draft
        s.execute(text("DELETE FROM deal_email_drafts WHERE id = :id"), {"id": draft_id})
        s.commit()
