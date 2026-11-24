"""Database table models.

Import every table model here so ``SQLModel.metadata.create_all`` sees them.
"""

from app.models.campaign import Campaign
from app.models.listing import Listing, ListingRevision
from app.models.notification import Notification
from app.models.user import User

__all__ = [
    "Campaign",
    "Listing",
    "ListingRevision",
    "Notification",
    "User",
]
