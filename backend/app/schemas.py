from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    name: str
    email: str
    role: str


class UserOut(BaseModel):
    id: int
    email: str  # email or Aadhaar login id
    name: str
    role: str

    class Config:
        from_attributes = True


class LeadIn(BaseModel):
    name: str = ""
    phone: str = ""
    email: str = ""
    company: str = ""
    city: str = ""
    district: str = ""
    state: str = ""
    source: str = "manual"
    status: str = "new"
    notes: str = ""


class LeadOut(LeadIn):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class LeadPage(BaseModel):
    items: list[LeadOut]
    total: int
    page: int
    page_size: int
    status_counts: dict[str, int] = {}


class EmployeeIn(BaseModel):
    code: Optional[str] = None
    name: str
    phone: str = ""
    whatsapp: str = ""
    email: str = ""
    dob: Optional[date] = None
    gender: str = ""
    blood_group: str = ""
    aadhaar: str = ""
    pan: str = ""
    languages: str = ""
    address: str = ""
    state: str = ""
    district: str = ""
    city: str = ""
    pin: str = ""
    emergency_name: str = ""
    emergency_phone: str = ""
    emergency_relation: str = ""
    company: str = "YesWeigh"
    branch: str = ""
    department: str = ""
    designation: str = ""
    reporting_manager: str = ""
    join_date: Optional[date] = None
    employment_type: str = "Permanent"
    shift: str = "General shift"
    working_hours: str = ""
    weekly_holiday: str = ""
    salary: Optional[Decimal] = None
    probation_months: Optional[int] = None
    adms_employee_code: str = ""
    adms_device: str = ""
    status: str = "Active"
    crm_role: str = "Employee"
    account_status: str = ""
    branch_access: str = "Assigned branch only"
    lead_visibility: str = "Assigned records only"
    notes: str = ""
    enable_crm_login: bool = False
    login_email: str = ""
    temporary_password: str = ""
    password: str = ""


class EmployeeListOut(BaseModel):
    id: int
    code: str
    name: str
    phone: str
    email: str
    department: str
    designation: str
    branch: str
    join_date: Optional[date] = None
    status: str
    adms_employee_code: str = ""
    photo_url: str = ""
    account_status: str = ""
    crm_role: str = ""
    created_at: datetime

    class Config:
        from_attributes = True


class EmployeeOut(EmployeeListOut):
    whatsapp: str = ""
    dob: Optional[date] = None
    gender: str = ""
    blood_group: str = ""
    aadhaar: str = ""
    pan: str = ""
    languages: str = ""
    address: str = ""
    state: str = ""
    district: str = ""
    city: str = ""
    pin: str = ""
    emergency_name: str = ""
    emergency_phone: str = ""
    emergency_relation: str = ""
    company: str = ""
    reporting_manager: str = ""
    employment_type: str = ""
    shift: str = ""
    working_hours: str = ""
    weekly_holiday: str = ""
    salary: Optional[Decimal] = None
    probation_months: Optional[int] = None
    adms_device: str = ""
    user_id: Optional[int] = None
    branch_access: str = ""
    lead_visibility: str = ""
    notes: str = ""
    updated_at: Optional[datetime] = None


class EmployeeStatsOut(BaseModel):
    total: int
    active: int
    present_today: int
    absent_today: int
    on_leave: int


class EmployeePageOut(BaseModel):
    items: list[EmployeeListOut]
    total: int
    page: int
    page_size: int
    stats: EmployeeStatsOut


class EmployeeStatusIn(BaseModel):
    status: str


class ResetPasswordIn(BaseModel):
    temporary_password: str = ""


class EmployeeDocumentOut(BaseModel):
    id: int
    employee_id: int
    doc_type: str
    file_name: str
    mime_type: str
    file_size: int
    verification_status: str
    created_at: datetime

    class Config:
        from_attributes = True


class EmployeeTargetIn(BaseModel):
    target_type: str
    target_value: str = ""
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    notes: str = ""


class EmployeeTargetOut(EmployeeTargetIn):
    id: int
    employee_id: int
    created_at: datetime

    class Config:
        from_attributes = True


class EmployeeActivityOut(BaseModel):
    id: int
    employee_id: int
    activity_type: str
    summary: str
    created_at: datetime

    class Config:
        from_attributes = True


class WorkIn(BaseModel):
    title: str
    description: str = ""
    status: str = "todo"
    priority: str = "medium"
    lead_id: Optional[int] = None
    assignee_id: Optional[int] = None
    due_date: Optional[date] = None


class WorkOut(WorkIn):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DashboardOut(BaseModel):
    leads_total: int
    leads_new: int
    work_open: int
    employees_active: int
    modules: list[str] = Field(default_factory=list)
