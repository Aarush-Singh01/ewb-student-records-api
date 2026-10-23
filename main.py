"""Student records API for the EWB Edu Tech internship assignment."""

from contextlib import asynccontextmanager, contextmanager
from datetime import date
import os
from pathlib import Path
import re
import sqlite3
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path as PathParam, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator


DATABASE_PATH = Path(os.getenv("STUDENTS_DB_PATH", "students.db"))
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PHONE_PATTERN = re.compile(r"^[0-9+().\-\s]{7,25}$")


def connect_db() -> sqlite3.Connection:
    """Open a database connection with named columns and foreign key checks."""
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def db_session():
    """Commit successful work and always close the short-lived connection."""
    connection = connect_db()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db_session() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS students (
                student_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                date_of_birth TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                course TEXT,
                address TEXT,
                enrollment_date TEXT,
                CHECK (email IS NOT NULL OR phone IS NOT NULL)
            )
            """
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(
    title="Student Records API",
    description="A small FastAPI and SQLite CRUD application for student records.",
    version="1.0.0",
    lifespan=lifespan,
)


class StudentFields(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    date_of_birth: date
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=25)
    course: str | None = Field(default=None, max_length=120)
    address: str | None = Field(default=None, max_length=300)
    enrollment_date: date | None = None

    @model_validator(mode="after")
    def validate_student(self):
        if self.date_of_birth > date.today():
            raise ValueError("date_of_birth cannot be in the future")
        if self.email is not None and not EMAIL_PATTERN.fullmatch(self.email):
            raise ValueError("email must be a valid email address")
        if self.phone is not None and not PHONE_PATTERN.fullmatch(self.phone):
            raise ValueError("phone must contain 7 to 25 digits or common phone symbols")
        if not self.email and not self.phone:
            raise ValueError("at least one contact field (email or phone) is required")
        if self.enrollment_date is not None and self.enrollment_date < self.date_of_birth:
            raise ValueError("enrollment_date cannot be before date_of_birth")
        return self


class StudentCreate(StudentFields):
    pass


class StudentUpdate(BaseModel):
    """Partial update input; omitted fields retain their stored values."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    date_of_birth: date | None = None
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=25)
    course: str | None = Field(default=None, max_length=120)
    address: str | None = Field(default=None, max_length=300)
    enrollment_date: date | None = None

    @model_validator(mode="after")
    def reject_empty_patch(self):
        if not self.model_fields_set:
            raise ValueError("provide at least one field to update")
        return self


class Student(StudentFields):
    student_id: int


def row_to_student(row: sqlite3.Row) -> Student:
    return Student(
        student_id=row["student_id"],
        name=row["name"],
        date_of_birth=row["date_of_birth"],
        email=row["email"],
        phone=row["phone"],
        course=row["course"],
        address=row["address"],
        enrollment_date=row["enrollment_date"],
    )


def get_student_row(student_id: int) -> sqlite3.Row:
    with db_session() as connection:
        row = connection.execute(
            "SELECT * FROM students WHERE student_id = ?", (student_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Student not found")
    return row


@app.get("/health", tags=["system"])
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/students", response_model=Student, status_code=status.HTTP_201_CREATED)
def create_student(student: StudentCreate) -> Student:
    values = student.model_dump(mode="json")
    with db_session() as connection:
        cursor = connection.execute(
            """INSERT INTO students
               (name, date_of_birth, email, phone, course, address, enrollment_date)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            tuple(values[key] for key in (
                "name", "date_of_birth", "email", "phone", "course", "address", "enrollment_date"
            )),
        )
        row = connection.execute(
            "SELECT * FROM students WHERE student_id = ?", (cursor.lastrowid,)
        ).fetchone()
    return row_to_student(row)


@app.get("/students", response_model=list[Student])
def list_students(
    name: Annotated[str | None, Query(max_length=120)] = None,
    course: Annotated[str | None, Query(max_length=120)] = None,
    born_after: date | None = None,
    born_before: date | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Student]:
    if born_after and born_before and born_after > born_before:
        raise HTTPException(status_code=422, detail="born_after must be on or before born_before")

    conditions: list[str] = []
    parameters: list[object] = []
    if name:
        conditions.append("name LIKE ? COLLATE NOCASE")
        parameters.append(f"%{name.strip()}%")
    if course:
        conditions.append("course LIKE ? COLLATE NOCASE")
        parameters.append(f"%{course.strip()}%")
    if born_after:
        conditions.append("date_of_birth > ?")
        parameters.append(born_after.isoformat())
    if born_before:
        conditions.append("date_of_birth < ?")
        parameters.append(born_before.isoformat())

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    parameters.extend((limit, offset))
    with db_session() as connection:
        rows = connection.execute(
            f"SELECT * FROM students {where_clause} ORDER BY student_id LIMIT ? OFFSET ?",
            parameters,
        ).fetchall()
    return [row_to_student(row) for row in rows]


@app.get("/students/{student_id}", response_model=Student)
def read_student(student_id: Annotated[int, PathParam(ge=1)]) -> Student:
    return row_to_student(get_student_row(student_id))


@app.put("/students/{student_id}", response_model=Student)
def replace_student(student_id: Annotated[int, PathParam(ge=1)], student: StudentCreate) -> Student:
    get_student_row(student_id)
    values = student.model_dump(mode="json")
    with db_session() as connection:
        connection.execute(
            """UPDATE students SET name = ?, date_of_birth = ?, email = ?, phone = ?,
               course = ?, address = ?, enrollment_date = ? WHERE student_id = ?""",
            (*values.values(), student_id),
        )
        row = connection.execute(
            "SELECT * FROM students WHERE student_id = ?", (student_id,)
        ).fetchone()
    return row_to_student(row)


@app.patch("/students/{student_id}", response_model=Student)
def update_student(student_id: Annotated[int, PathParam(ge=1)], changes: StudentUpdate) -> Student:
    current = get_student_row(student_id)
    merged = {key: current[key] for key in (
        "name", "date_of_birth", "email", "phone", "course", "address", "enrollment_date"
    )}
    merged.update(changes.model_dump(exclude_unset=True, mode="json"))
    # Revalidate the complete record so partial updates cannot break contact/date rules.
    validated = StudentCreate.model_validate(merged)
    values = validated.model_dump(mode="json")
    with db_session() as connection:
        connection.execute(
            """UPDATE students SET name = ?, date_of_birth = ?, email = ?, phone = ?,
               course = ?, address = ?, enrollment_date = ? WHERE student_id = ?""",
            (*values.values(), student_id),
        )
        row = connection.execute(
            "SELECT * FROM students WHERE student_id = ?", (student_id,)
        ).fetchone()
    return row_to_student(row)


@app.delete("/students/{student_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_student(student_id: Annotated[int, PathParam(ge=1)]) -> Response:
    with db_session() as connection:
        cursor = connection.execute("DELETE FROM students WHERE student_id = ?", (student_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Student not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
