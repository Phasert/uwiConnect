from sqlmodel import Field, SQLModel


class Listing(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    author_id: int = Field(index=True)
    kind: str = Field(index=True)
    title: str
    body: str
    status: str
    rejection_reason: str | None = None
    deletion_requested: bool = False
    company_name: str | None = None
    location: str | None = None
    application_instructions: str | None = None
    application_deadline: str | None = None
    faculty: str | None = None
    department: str | None = None
    event_date: str | None = None
    event_time: str | None = None
    category: str | None = None
    registration_information: str | None = None
    created_at: str


class ListingRevision(SQLModel, table=True):
    listing_id: int = Field(foreign_key="listing.id", primary_key=True)
    author_id: int
    status: str
    kind: str
    title: str
    body: str
    company_name: str | None = None
    location: str | None = None
    application_instructions: str | None = None
    application_deadline: str | None = None
    faculty: str | None = None
    department: str | None = None
    event_date: str | None = None
    event_time: str | None = None
    category: str | None = None
    registration_information: str | None = None
    requested_at: str
