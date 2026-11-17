"""Controlled vocabularies shared across the application."""

from enum import StrEnum


class ExceptionType(StrEnum):
    MISSING_CONTRIBUTION = "MISSING_CONTRIBUTION"
    INCORRECT_CONTRIBUTION_RATE = "INCORRECT_CONTRIBUTION_RATE"
    CONTRIBUTION_AFTER_TERMINATION = "CONTRIBUTION_AFTER_TERMINATION"
    DUPLICATE_OR_INVALID_RECORD = "DUPLICATE_OR_INVALID_RECORD"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ExceptionStatus(StrEnum):
    OPEN = "open"
    UNDER_INVESTIGATION = "under_investigation"
    MANUAL_REVIEW = "manual_review"
    CLOSED = "closed"


class BatchStatus(StrEnum):
    PROCESSING = "processing"
    PROCESSED = "processed"


class ValidationStatus(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"


class EnrollmentStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class DataClassification(StrEnum):
    SYNTHETIC_DEMO = "SYNTHETIC_DEMO"


class EvidenceType(StrEnum):
    PAYROLL_ROW = "payroll_row"
    PARTICIPANT = "participant"
    PLAN_ENROLLMENT = "plan_enrollment"
    PLAN_RULE = "plan_rule"
    CALCULATION = "calculation"
    VALIDATION_ERRORS = "validation_errors"


class ReasonCode(StrEnum):
    """Why a row was flagged. Row-level validation codes and reconciliation codes."""

    # Row validation
    MALFORMED_ROW = "MALFORMED_ROW"
    MISSING_VALUE = "MISSING_VALUE"
    MALFORMED_EMPLOYEE_REF = "MALFORMED_EMPLOYEE_REF"
    UNKNOWN_PARTICIPANT = "UNKNOWN_PARTICIPANT"
    MALFORMED_DATE = "MALFORMED_DATE"
    INVALID_PAY_PERIOD = "INVALID_PAY_PERIOD"
    MALFORMED_AMOUNT = "MALFORMED_AMOUNT"
    NEGATIVE_AMOUNT = "NEGATIVE_AMOUNT"
    DUPLICATE_ROW = "DUPLICATE_ROW"
    # Reconciliation
    CONTRIBUTION_ZERO_OR_BLANK = "CONTRIBUTION_ZERO_OR_BLANK"
    CONTRIBUTION_RATE_MISMATCH = "CONTRIBUTION_RATE_MISMATCH"
    NO_ACTIVE_ENROLLMENT = "NO_ACTIVE_ENROLLMENT"
    PERIOD_STARTS_AFTER_TERMINATION = "PERIOD_STARTS_AFTER_TERMINATION"


class FileRejectionCode(StrEnum):
    """Whole-file rejections: nothing is stored for a rejected file."""

    EMPTY_FILE = "EMPTY_FILE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    TOO_MANY_ROWS = "TOO_MANY_ROWS"
    NOT_UTF8 = "NOT_UTF8"
    MALFORMED_CSV = "MALFORMED_CSV"
    MISSING_COLUMNS = "MISSING_COLUMNS"
    DUPLICATE_COLUMNS = "DUPLICATE_COLUMNS"
    RESTRICTED_COLUMNS = "RESTRICTED_COLUMNS"
    UNSUPPORTED_COLUMNS = "UNSUPPORTED_COLUMNS"
    MIXED_PAY_PERIODS = "MIXED_PAY_PERIODS"
    RESTRICTED_CONTENT = "RESTRICTED_CONTENT"
