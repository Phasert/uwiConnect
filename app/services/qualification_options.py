from sqlmodel import Session, select

from app.database import engine
from app.models.qualification_department import QualificationDepartment

DEPARTMENTS = {
    "Science and Technology": ("Chemistry", "Computing and Information Technology", "Mathematics and Statistics"),
    "Engineering": ("Chemical Engineering", "Civil & Environmental Engineering", "Electrical & Computer Engineering"),
    "Social Sciences": ("Behavioural Sciences", "Economics", "Management Studies"),
}


def seed_qualification_options(session: Session) -> None:
    for faculty, departments in DEPARTMENTS.items():
        for department in departments:
            if session.get(QualificationDepartment, (faculty, department)) is None:
                session.add(QualificationDepartment(faculty=faculty, department=department))
    session.commit()


def qualification_options() -> dict[str, list[str]]:
    with Session(engine) as session:
        rows = session.exec(
            select(QualificationDepartment).order_by(
                QualificationDepartment.faculty, QualificationDepartment.department
            )
        ).all()
    options = {}
    for row in rows:
        options.setdefault(row.faculty, []).append(row.department)
    return options or {faculty: list(departments) for faculty, departments in DEPARTMENTS.items()}
