from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ContactInquiryCreate(BaseModel):
    """Public landing-page Contact Us payload (lean validation)."""

    name: str = Field(..., min_length=2, max_length=120)
    company: Optional[str] = Field(None, max_length=120)
    # Plain str + regex is cheaper than EmailStr/email-validator on the hot path
    email: str = Field(..., min_length=5, max_length=255)
    message: str = Field(..., min_length=10, max_length=4000)
    website: Optional[str] = Field(None, max_length=200)
    source: Optional[str] = Field(default="landing", max_length=50)

    @field_validator("name", "company", "email", "message", "website", "source", mode="before")
    @classmethod
    def strip_strings(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("company", "website", mode="before")
    @classmethod
    def empty_to_none(cls, v):
        if v == "":
            return None
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if "@" not in v or "." not in v.rsplit("@", 1)[-1]:
            raise ValueError("Please enter a valid email.")
        return v.lower()
