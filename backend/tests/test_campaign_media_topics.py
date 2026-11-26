"""Campaign media topic/title policy: uid unique, display name may repeat."""
from __future__ import annotations

from app.models import CampaignMedia


def test_campaign_media_uid_unique_title_not_unique():
  title_col = CampaignMedia.__table__.c.title
  uid_col = CampaignMedia.__table__.c.uid
  stored_col = CampaignMedia.__table__.c.stored_name
  assert not title_col.unique
  assert uid_col.unique
  assert stored_col.unique