from fastapi import Request


def flash(request: Request, message: str, type: str = "success") -> None:
    messages = list(request.session.get("_messages", []))
    messages.append({"message": message, "type": type})
    request.session["_messages"] = messages


def get_flashed_messages(request: Request):
   return request.session.pop("_messages") if "_messages" in request.session else []
