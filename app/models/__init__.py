"""Database table models.

Import every table model here so ``SQLModel.metadata.create_all`` sees them.
"""

from app.models.campaign import Campaign
from app.models.email_delivery import EmailDelivery
from app.models.listing import Listing, ListingRevision
from app.models.notification import Notification
from app.models.user import User
from app.models.verification_token import VerificationToken

__all__ = [
    "Campaign",
    "EmailDelivery",
    "Listing",
    "ListingRevision",
    "Notification",
    "User",
    "VerificationToken",
]
