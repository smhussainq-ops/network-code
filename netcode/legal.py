"""Versioned legal document contract for customer software access."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class LegalDocument:
    document_id: str
    title: str
    version: str
    effective_date: str
    sha256: str
    public_url: str

    def as_dict(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "title": self.title,
            "version": self.version,
            "effective_date": self.effective_date,
            "sha256": self.sha256,
            "public_url": self.public_url,
        }


def software_agreement() -> LegalDocument:
    base_url = os.environ.get("REZONANCE_LEGAL_BASE_URL", "https://rezonancenetworks.com").rstrip("/")
    return LegalDocument(
        document_id="community-software-license-and-services-agreement",
        title="Software License and Services Agreement",
        version="community-2026-07-29-v1",
        effective_date="2026-07-29",
        sha256="831c00fce61c0ad0839ba6e580f4de4cf5be9c65805dfaa69d84c0e48ba44495",
        public_url=f"{base_url}/software-agreement.html",
    )


def privacy_notice_url() -> str:
    base_url = os.environ.get("REZONANCE_LEGAL_BASE_URL", "https://rezonancenetworks.com").rstrip("/")
    return f"{base_url}/privacy.html"
