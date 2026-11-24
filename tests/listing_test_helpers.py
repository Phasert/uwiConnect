def complete_listing_payload(payload: dict) -> dict:
    data = payload.copy()
    if data["kind"] == "job":
        data.setdefault("company_name", "Test Company")
        data.setdefault("location", "St. Augustine")
        data.setdefault("application_instructions", "Apply by email")
        data.setdefault("faculty", "Science and Technology")
    else:
        data.setdefault("event_date", "2099-06-15")
        data.setdefault("event_time", "18:00")
        data.setdefault("location", "UWI Campus")
        data.setdefault("category", "Networking")
    return data
