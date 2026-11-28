from sqlmodel import Field, SQLModel


class QualificationDepartment(SQLModel, table=True):
    faculty: str = Field(primary_key=True)
    department: str = Field(primary_key=True)
