-- Sewa Setu Old Age Pension Portal - schema
-- NOTE: seed.sql (shipped separately) loads after this and contains the
-- production data. Do not re-run against a live database.

CREATE TABLE IF NOT EXISTS applications (
    id              SERIAL PRIMARY KEY,
    application_no  VARCHAR(20),
    applicant_name  VARCHAR(100),
    mobile          VARCHAR(15),
    dob             DATE,
    gender          VARCHAR(10),
    marital_status  VARCHAR(20),
    husband_name    VARCHAR(100),
    husband_employer VARCHAR(100),
    village         VARCHAR(100),
    block           VARCHAR(50),
    bank_account    VARCHAR(30),
    ifsc            VARCHAR(15),
    doc_path        VARCHAR(200),
    status          VARCHAR(20) DEFAULT 'PENDING',
    submitted_at    TIMESTAMP,
    decided_at      TIMESTAMP,
    decided_by      VARCHAR(50)
);

-- Status portal accounts (created at submission time)
CREATE TABLE IF NOT EXISTS portal_users (
    mobile        VARCHAR(15) PRIMARY KEY,
    password_hash VARCHAR(80)
);

CREATE TABLE IF NOT EXISTS otps (
    id         SERIAL PRIMARY KEY,
    mobile     VARCHAR(15),
    code       VARCHAR(6),
    created_at TIMESTAMP
);

-- Corrections are retained independently so a citizen can fix a pending form
-- without deleting their original submission or creating a duplicate.
CREATE TABLE IF NOT EXISTS application_corrections (
    id                  SERIAL PRIMARY KEY,
    application_id      INTEGER NOT NULL REFERENCES applications(id),
    corrected_at        TIMESTAMP NOT NULL,
    corrected_by_mobile VARCHAR(15) NOT NULL,
    previous_values     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS applications_status_submitted_idx ON applications (status, submitted_at);
CREATE INDEX IF NOT EXISTS applications_mobile_idx ON applications (mobile);
CREATE INDEX IF NOT EXISTS applications_application_no_idx ON applications (application_no);
CREATE INDEX IF NOT EXISTS applications_name_lower_idx ON applications (lower(applicant_name));
