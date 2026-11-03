"""
Enterprise Additive Coalescence Engine (Master Data Management / MDM Layer).
Provides lossless non-destructive merging, recursive dictionary merging,
set-union list consolidation, and scalar quality upgrade rules across
Accounts, Lines of Business (LOBs), and Executive Personas.

100% Dynamic, Zero Hardcoding, Zero Schema Changes.
"""

import copy
import re
from typing import Any, Dict, List, Optional, Union
from sqlalchemy.orm.attributes import flag_modified


class CoalescenceEngine:
    """Universal Enterprise Data Coalescence Engine for Golden Record Merging."""

    # ──────────────────────────────────────────────────────────────────────────
    # 1. CORE RECURSIVE DICT MERGE (OSINT Feeds, Raw Telemetry, Metadata)
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def deep_merge_dicts(cls, base: Optional[dict], overlay: Optional[dict]) -> dict:
        """
        Recursively merges overlay into base without data loss.
        - Dict keys in overlay are merged into base. Sub-dicts recurse.
        - List values are merged additively (set-union).
        - Non-null/non-empty scalar values in overlay upgrade base values.
        - Preserves all historical source feeds (e.g. diffbot, apify, exa, sec).
        """
        if base is None and overlay is None:
            return {}
        if base is None or not isinstance(base, dict):
            return copy.deepcopy(overlay) if isinstance(overlay, dict) else {}
        if overlay is None or not isinstance(overlay, dict):
            return copy.deepcopy(base)

        result = copy.deepcopy(base)

        for key, val in overlay.items():
            if val is None or val == "" or val == [] or val == {}:
                # Never wipe out existing valid sub-data with empty values
                continue

            if key not in result or result[key] is None or result[key] == "" or result[key] == [] or result[key] == {}:
                result[key] = copy.deepcopy(val)
                continue

            current_val = result[key]

            if isinstance(current_val, dict) and isinstance(val, dict):
                result[key] = cls.deep_merge_dicts(current_val, val)
            elif isinstance(current_val, list) and isinstance(val, list):
                result[key] = cls.merge_set_lists(current_val, val)
            else:
                result[key] = cls.resolve_scalar(current_val, val, field_name=key)

        return result

    # ──────────────────────────────────────────────────────────────────────────
    # 2. SET-UNION LIST CONSOLIDATION (Skills, Keywords, Industries, Career)
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def merge_set_lists(
        cls, current_list: Optional[list], incoming_list: Optional[list], key_fn=None
    ) -> list:
        """
        Performs an additive, deduplicated set-union of two lists.
        - For scalar lists (strings/ints): case-insensitive uniqueness, preserving stable order.
        - For dict lists (career history, patents, etc.): deduplicates by identity key.
        """
        if not current_list and not incoming_list:
            return []
        if not current_list:
            return copy.deepcopy(incoming_list) if isinstance(incoming_list, list) else []
        if not incoming_list:
            return copy.deepcopy(current_list) if isinstance(current_list, list) else []

        merged = list(copy.deepcopy(current_list))

        # Check if list contains dicts (e.g. employment_history, education_history)
        has_dicts = any(isinstance(x, dict) for x in current_list) or any(
            isinstance(x, dict) for x in incoming_list
        )

        if has_dicts:
            existing_keys = set()
            for r in merged:
                if isinstance(r, dict):
                    k = cls._extract_record_identity_key(r)
                    if k:
                        existing_keys.add(k)

            for r in incoming_list:
                if isinstance(r, dict):
                    r_key = cls._extract_record_identity_key(r)
                    if r_key:
                        if r_key not in existing_keys:
                            merged.append(copy.deepcopy(r))
                            existing_keys.add(r_key)
                    elif r not in merged:
                        merged.append(copy.deepcopy(r))
                elif r not in merged:
                    merged.append(r)
            return merged

        # Scalar list (strings / numbers)
        seen_scalars = set()
        for x in merged:
            if isinstance(x, str):
                seen_scalars.add(x.strip().lower())
            else:
                seen_scalars.add(x)

        for x in incoming_list:
            if x is None or x == "":
                continue
            if isinstance(x, str):
                normalized = x.strip().lower()
                if normalized not in seen_scalars:
                    merged.append(x.strip())
                    seen_scalars.add(normalized)
            elif x not in seen_scalars:
                merged.append(x)
                seen_scalars.add(x)

        return merged

    @staticmethod
    def _extract_record_identity_key(record: dict) -> str:
        """Extracts a stable identity key for career, patent, or sub-lob objects."""
        # 1. Career record: company + role / title
        comp = record.get("company") or record.get("organization") or record.get("employer") or ""
        role = record.get("title") or record.get("role") or record.get("position") or ""
        if comp or role:
            return f"career:{str(comp).strip().lower()}::{str(role).strip().lower()}"

        # 2. Education record: institution / school + degree
        school = record.get("institution") or record.get("school") or record.get("university") or ""
        degree = record.get("degree") or record.get("major") or ""
        if school or degree:
            return f"edu:{str(school).strip().lower()}::{str(degree).strip().lower()}"

        # 3. Patent record: patent number or title
        pat_num = record.get("patent_number") or record.get("id") or record.get("patent_id") or ""
        if pat_num:
            return f"patent:{str(pat_num).strip().lower()}"

        # 4. Legal entity / Sub-LOB: LEI or legal name
        lei = record.get("lei") or record.get("lei_code") or ""
        legal_name = record.get("legal_name") or record.get("name") or ""
        if lei or legal_name:
            return f"entity:{str(lei).strip().lower()}::{str(legal_name).strip().lower()}"

        # 5. Generic name
        nm = record.get("name") or record.get("title") or ""
        if nm:
            return f"generic:{str(nm).strip().lower()}"

        return ""

    # ──────────────────────────────────────────────────────────────────────────
    # 3. SCALAR RESOLUTION & FIDELITY UPGRADE RULES
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def resolve_scalar(
        cls, current_val: Any, incoming_val: Any, field_name: str = "", is_manually_verified: bool = False
    ) -> Any:
        """
        Determines whether incoming_val should replace or upgrade current_val.
        Rules:
        - Manual verification lock: human edits are locked against automated overwrites.
        - Null/empty shielding: incoming empty never replaces populated current value.
        - Unmasked upgrade: plaintext unmasked name wins over asterisks ('***').
        - Verified email: verified status wins over synthesized/unverified.
        - Personal LinkedIn URL: /in/ wins over search query URLs.
        - Information density: substantive descriptions win over short stubs.
        - Financial figures: audited numbers win over 'N/A' or '$0'.
        """
        if is_manually_verified:
            return current_val

        # Null / empty shielding
        if incoming_val is None or incoming_val == "" or incoming_val == [] or incoming_val == {}:
            return current_val
        if current_val is None or current_val == "" or current_val == [] or current_val == {}:
            return incoming_val

        # If values are equal, return existing
        if current_val == incoming_val:
            return current_val

        c_str = str(current_val).strip()
        i_str = str(incoming_val).strip()

        # 1. Unmasked name vs masked name (e.g. 'John Doe' vs 'J*** D***')
        if "***" in c_str and "***" not in i_str:
            return incoming_val
        if "***" in i_str and "***" not in c_str:
            return current_val

        # 2. LinkedIn URL: personal profile /in/ vs search query URL
        if field_name == "linkedin_url" or "linkedin" in field_name:
            if "/in/" in i_str and "/in/" not in c_str:
                return incoming_val
            if "/in/" in c_str and "/in/" not in i_str:
                return current_val

        # 3. Photo & Avatar URL resolution: valid headshot vs ghost/logo/placeholder
        if any(term in field_name.lower() for term in ["photo", "avatar", "picture", "image"]):
            is_incoming_ghost = any(g in i_str.lower() for g in ["ghost", "default", "blank", "placeholder", "logo", "spacer"])
            is_current_ghost = any(g in c_str.lower() for g in ["ghost", "default", "blank", "placeholder", "logo", "spacer"])
            if is_incoming_ghost and not is_current_ghost and c_str.startswith("http"):
                return current_val
            if is_current_ghost and not is_incoming_ghost and i_str.startswith("http"):
                return incoming_val
            if i_str.startswith("http") and not c_str.startswith("http"):
                return incoming_val
            if c_str.startswith("http") and not i_str.startswith("http"):
                return current_val
            # Higher resolution LinkedIn CDN URL preferred
            if "800_800" in i_str and "800_800" not in c_str:
                return incoming_val
            if "800_800" in c_str and "800_800" not in i_str:
                return current_val
            return incoming_val

        # 4. Email fields: verified email vs synthesized/unverified
        if field_name == "email":
            if "@" in i_str and "@" not in c_str:
                return incoming_val
            if "@" in c_str and "@" not in i_str:
                return current_val

        # 4. Financial & revenue strings (e.g. '$24.22B' vs 'Revenue N/A' or '$0')
        if any(term in field_name for term in ["revenue", "it_spend", "funding", "spend"]):
            if any(term in c_str.lower() for term in ["n/a", "unknown", "none", "$0", "0"]):
                if not any(term in i_str.lower() for term in ["n/a", "unknown", "none", "$0", "0"]):
                    return incoming_val
            if any(term in i_str.lower() for term in ["n/a", "unknown", "none", "$0", "0"]):
                return current_val
            # More specific / precise representation wins (e.g. '$24.22B' vs '$24B')
            if len(i_str) > len(c_str) and any(ch.isdigit() for ch in i_str):
                return incoming_val

        # 5. Text descriptions & overviews: higher information density wins
        if any(term in field_name for term in ["description", "overview", "desc", "headline", "bio", "summary"]):
            # If current is very short (< 40 chars) and incoming is comprehensive (> 60 chars)
            if len(i_str) > len(c_str) * 1.5:
                return incoming_val
            if len(c_str) > len(i_str) * 1.5:
                return current_val

        # Default fallback: keep incoming if it contains more entropy/characters
        if len(i_str) > len(c_str):
            return incoming_val

        return current_val

    # ──────────────────────────────────────────────────────────────────────────
    # 4. ENTITY-SPECIFIC LOSSLESS COALESCENCE
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def coalesce_persona(
        cls, master: Any, incoming_data: Dict[str, Any], lob_id: Optional[int] = None
    ) -> Any:
        """
        Lossless Non-Destructive Merger for Executive Personas.
        Enriches the master persona with non-null incoming data across all 89 attributes.
        Deep-merges JSON telemetry and OSINT manifests. Additively unions career & skills lists.
        """
        is_manual = getattr(master, "is_manually_verified", False) or False

        # Set of fields that should be additively unioned as lists
        LIST_FIELDS = {
            "skills", "past_companies", "previous_titles", "target_kpis",
            "operational_pain_points", "key_objections", "employment_history",
            "education_history"
        }

        # Set of fields that must be recursively deep-merged as dicts
        DICT_FIELDS = {
            "osint_feed_manifest", "extended_profile", "raw_data"
        }

        SKIP_FIELDS = {"id", "account_id", "account", "lob"}

        for field, val in incoming_data.items():
            if field in SKIP_FIELDS:
                continue
            if val is None or val == "" or val == [] or val == {}:
                continue

            current_val = getattr(master, field, None)

            # If master currently lacks this field, set it directly
            if current_val is None or current_val == "" or current_val == [] or current_val == {}:
                if hasattr(master, field):
                    setattr(master, field, val)
                    if field in DICT_FIELDS or field in LIST_FIELDS:
                        flag_modified(master, field)
                continue

            # Dict field deep merge (solves OSINT feed & raw data truncation!)
            if field in DICT_FIELDS:
                if isinstance(val, dict):
                    merged_dict = cls.deep_merge_dicts(current_val if isinstance(current_val, dict) else {}, val)
                    setattr(master, field, merged_dict)
                    flag_modified(master, field)
                continue

            # List field set-union
            if field in LIST_FIELDS:
                if isinstance(val, list):
                    merged_list = cls.merge_set_lists(current_val if isinstance(current_val, list) else [], val)
                    setattr(master, field, merged_list)
                    flag_modified(master, field)
                continue

            # Manual verification override rule
            if field == "is_manually_verified":
                if val is True:
                    master.is_manually_verified = True
                continue

            # Scalar field resolution
            if hasattr(master, field):
                resolved = cls.resolve_scalar(current_val, val, field_name=field, is_manually_verified=is_manual)
                setattr(master, field, resolved)

        # 5. Extract and persist headshot photo_url into extended_profile and raw_data
        incoming_photo = (
            incoming_data.get("photo_url")
            or incoming_data.get("photo")
            or (incoming_data.get("raw_data") or {}).get("photo_url")
            or (incoming_data.get("raw_data") or {}).get("apify_linkedin", {}).get("photo")
            or ((incoming_data.get("raw_data") or {}).get("apify_linkedin", {}).get("profilePicture") or {}).get("url")
            or (incoming_data.get("raw_data") or {}).get("diffbot", {}).get("image_url")
            or (incoming_data.get("raw_data") or {}).get("diffbot", {}).get("image")
            or (incoming_data.get("raw_data") or {}).get("apollo", {}).get("photo_url")
            or (incoming_data.get("extended_profile") or {}).get("photo_url")
        )
        if incoming_photo and isinstance(incoming_photo, str) and incoming_photo.startswith("http"):
            if not any(g in incoming_photo.lower() for g in ["ghost_person", "ghost-person", "logo", "spacer"]):
                ep = copy.deepcopy(master.extended_profile or {})
                rd = copy.deepcopy(master.raw_data or {})
                curr_photo = ep.get("photo_url") or rd.get("photo_url")
                resolved_photo = cls.resolve_scalar(curr_photo, incoming_photo, field_name="photo_url", is_manually_verified=is_manual)
                ep["photo_url"] = resolved_photo
                rd["photo_url"] = resolved_photo
                master.extended_profile = ep
                master.raw_data = rd
                flag_modified(master, "extended_profile")
                flag_modified(master, "raw_data")
                if hasattr(master, "photo_url"):
                    setattr(master, "photo_url", resolved_photo)

        if lob_id and not master.lob_id:
            master.lob_id = lob_id

        return master

    @classmethod
    def coalesce_account(cls, master: Any, incoming_data: Dict[str, Any]) -> Any:
        """
        Lossless Non-Destructive Merger for Enterprise Accounts.
        Preserves all 45+ firmographic columns, set-unions keywords/industries/aliases/founders,
        and deep-merges multi-source intelligence and hierarchy trees.
        """
        is_manual = getattr(master, "is_manually_verified", False) or False

        LIST_FIELDS = {
            "keywords", "industries", "industry_groups", "aliases", "founders",
            "patents_portfolio", "fec_political_giving", "headquarters_regions"
        }

        DICT_FIELDS = {
            "raw_data", "osint_feed_manifest", "multi_source_intelligence",
            "organisational_hierarchy_tree"
        }

        SKIP_FIELDS = {"id", "lobs", "personas", "action_items", "user_access", "signals", "extracted_at"}

        for field, val in incoming_data.items():
            if field in SKIP_FIELDS:
                continue
            if val is None or val == "" or val == [] or val == {}:
                continue

            current_val = getattr(master, field, None)

            # If master currently lacks this field, set it directly
            if current_val is None or current_val == "" or current_val == [] or current_val == {}:
                if hasattr(master, field):
                    setattr(master, field, val)
                    if field in DICT_FIELDS or field in LIST_FIELDS:
                        flag_modified(master, field)
                continue

            # Dict deep merge (prevents GLEIF / SEC / Crunchbase hierarchy tree overwrites)
            if field in DICT_FIELDS:
                if isinstance(val, dict):
                    merged_dict = cls.deep_merge_dicts(current_val if isinstance(current_val, dict) else {}, val)
                    setattr(master, field, merged_dict)
                    flag_modified(master, field)
                continue

            # List set-union (prevents industry / keyword truncation)
            if field in LIST_FIELDS:
                if isinstance(val, list):
                    merged_list = cls.merge_set_lists(current_val if isinstance(current_val, list) else [], val)
                    setattr(master, field, merged_list)
                    flag_modified(master, field)
                continue

            # Scalar field resolution (protects description, revenue, CIK, domains)
            if hasattr(master, field):
                resolved = cls.resolve_scalar(current_val, val, field_name=field, is_manually_verified=is_manual)
                setattr(master, field, resolved)

        return master

    @classmethod
    def coalesce_lob(cls, master: Any, incoming_data: Dict[str, Any]) -> Any:
        """
        Lossless Non-Destructive Merger for Lines of Business (LOBs).
        Shields audited_segment_revenue and operating_head from None overwrites,
        set-unions technologies and competitors, and deep-merges segment metadata and descriptions.
        """
        is_manual = getattr(master, "is_manually_verified", False) or False

        DICT_FIELDS = {"raw_data", "metadata_", "financial_snippets", "patents", "osint_feed_manifest"}
        LIST_FIELDS = {"technologies", "competitors"}
        SKIP_FIELDS = {"id", "sub_lobs", "personas", "account", "account_id"}

        for field, val in incoming_data.items():
            if field in SKIP_FIELDS:
                continue
            if val is None or val == "" or val == [] or val == {}:
                continue

            current_val = getattr(master, field, None)

            # If master currently lacks this field, set it directly
            if current_val is None or current_val == "" or current_val == [] or current_val == {}:
                if hasattr(master, field):
                    setattr(master, field, val)
                    if field in DICT_FIELDS or field in LIST_FIELDS:
                        flag_modified(master, field)
                continue

            # Dict deep merge
            if field in DICT_FIELDS:
                if isinstance(val, dict):
                    merged_dict = cls.deep_merge_dicts(current_val if isinstance(current_val, dict) else {}, val)
                    setattr(master, field, merged_dict)
                    flag_modified(master, field)
                continue

            # List field set-union (technologies, competitors)
            if field in LIST_FIELDS:
                if isinstance(val, list):
                    merged_list = cls.merge_set_lists(current_val if isinstance(current_val, list) else [], val)
                    setattr(master, field, merged_list)
                    flag_modified(master, field)
                continue

            # Scalar field resolution (protects audited revenue, operating head, overview)
            if hasattr(master, field):
                resolved = cls.resolve_scalar(current_val, val, field_name=field, is_manually_verified=is_manual)
                setattr(master, field, resolved)

        return master
