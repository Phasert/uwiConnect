"""Database table models.

Import every table model here so ``SQLModel.metadata.create_all`` sees them.
"""

from app.models.campaign import Campaign
from app.models.listing import Listing
from app.models.notification import Notification
from app.models.user import User
from app.models.verification_token import VerificationToken

__all__ = [
    "Campaign",
    "Listing",
    "Notification",
    "User",
    "VerificationToken",
]
