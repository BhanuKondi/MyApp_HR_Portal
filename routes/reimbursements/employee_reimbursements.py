from flask import Blueprint, flash, redirect, render_template, request, url_for

from models.db import db
from models.models import Company, ReimbursementRequest, ReimbursementType
from utils.authz import (
    ROLE_ACCOUNT_ADMIN,
    ROLE_ADMIN,
    ROLE_MANAGER,
    ROLE_USER,
    get_base_template_for_role,
    get_current_employee,
    get_current_role,
    require_roles,
)
from utils.company_service import get_active_companies
from utils.reimbursement_pdf import render_reimbursement_pdf
from utils.reimbursement_service import (
    ALLOWED_ATTACHMENT_ACCEPT,
    ALLOWED_ATTACHMENT_LABEL,
    STATUS_DRAFT,
    STATUS_PENDING_MANAGER,
    add_attachments,
    generate_request_no,
    get_or_create_reimbursement_config,
    parse_amount,
    parse_bill_date,
    record_action,
    resolve_manager_approver,
    seed_reimbursement_types,
)
from utils.workflow_email_service import send_reimbursement_submitted_email


employee_reimbursements_bp = Blueprint("employee_reimbursements", __name__, url_prefix="/employee/reimbursements")


@employee_reimbursements_bp.before_request
def enforce_employee_access():
    return require_roles(ROLE_USER, ROLE_MANAGER, ROLE_ADMIN, ROLE_ACCOUNT_ADMIN)


@employee_reimbursements_bp.route("")
def list_reimbursements():
    employee = get_current_employee()
    base_template = get_base_template_for_role(get_current_role())
    if not employee:
        flash("A linked employee profile is required to use reimbursements.", "warning")
        return render_template(
            "employee/reimbursements.html",
            base_template=base_template,
            employee=None,
            reimbursements=[],
            summary={"draft": 0, "pending": 0, "approved": 0, "paid": 0},
        )
    reimbursements = (
        ReimbursementRequest.query.filter_by(employee_id=employee.id)
        .order_by(ReimbursementRequest.created_at.desc(), ReimbursementRequest.id.desc())
        .all()
    )
    summary = {
        "draft": sum(1 for item in reimbursements if item.status == STATUS_DRAFT),
        "pending": sum(1 for item in reimbursements if item.status in {"pending_manager", "pending_finance"}),
        "approved": sum(1 for item in reimbursements if item.status == "approved_for_payment"),
        "paid": sum(1 for item in reimbursements if item.status == "paid"),
    }
    return render_template(
        "employee/reimbursements.html",
        base_template=base_template,
        employee=employee,
        reimbursements=reimbursements,
        summary=summary,
    )


@employee_reimbursements_bp.route("/new")
def new_reimbursement():
    employee = get_current_employee()
    if not employee:
        flash("A linked employee profile is required to create a reimbursement request.", "warning")
        return redirect(url_for("employee_reimbursements.list_reimbursements"))
    seed_reimbursement_types()
    config = get_or_create_reimbursement_config()
    types = ReimbursementType.query.filter_by(is_active=True).order_by(ReimbursementType.name.asc()).all()
    return render_template(
        "employee/reimbursement_form.html",
        base_template=get_base_template_for_role(get_current_role()),
        reimbursement=None,
        reimbursement_types=types,
        companies=get_active_companies(),
        config=config,
        allowed_attachment_accept=ALLOWED_ATTACHMENT_ACCEPT,
        allowed_attachment_label=ALLOWED_ATTACHMENT_LABEL,
    )


@employee_reimbursements_bp.route("/create", methods=["POST"])
def create_reimbursement():
    employee = get_current_employee()
    if not employee:
        flash("A linked employee profile is required to create a reimbursement request.", "danger")
        return redirect(url_for("employee_reimbursements.list_reimbursements"))
    config = get_or_create_reimbursement_config()
    manager_approver = resolve_manager_approver(employee, config)

    try:
        requested_amount = parse_amount(request.form.get("requested_amount"))
        bill_date = parse_bill_date(request.form.get("bill_date"))
        reimbursement_type_id = int(request.form.get("reimbursement_type_id"))
        company_id = int(request.form.get("company_id"))
        description = (request.form.get("description") or "").strip()
        if not description:
            raise ValueError("Description is required.")
        company = Company.query.filter_by(id=company_id, is_active=True).first()
        if not company:
            raise ValueError("Please select a valid company.")

        action = request.form.get("form_action", "draft")
        status = STATUS_DRAFT
        submitted_at = None
        if action == "submit":
            if not manager_approver:
                raise ValueError("No reimbursement approver is configured for this employee.")
            status = STATUS_PENDING_MANAGER
            submitted_at = db.func.now()

        request_obj = ReimbursementRequest(
            request_no=generate_request_no(),
            employee_id=employee.id,
            reimbursement_type_id=reimbursement_type_id,
            company_id=company.id,
            bill_date=bill_date,
            description=description,
            requested_amount=requested_amount,
            status=status,
            manager_approver_user_id=manager_approver.id if manager_approver else None,
            current_assignee_user_id=manager_approver.id if manager_approver and status == STATUS_PENDING_MANAGER else None,
            submitted_at=submitted_at,
        )
        db.session.add(request_obj)
        db.session.flush()

        attachment_count = add_attachments(request_obj, request.files.getlist("attachments"))
        if attachment_count == 0:
            raise ValueError("At least one bill attachment is required.")

        record_action(
            request_obj=request_obj,
            action_by_user_id=employee.user_id,
            action_type="submitted" if status == STATUS_PENDING_MANAGER else "created",
            from_status=None,
            to_status=status,
            comments=None,
        )
        db.session.commit()
        if status == STATUS_PENDING_MANAGER:
            send_reimbursement_submitted_email(request_obj)
        flash(
            "Reimbursement submitted for manager review." if status == STATUS_PENDING_MANAGER else "Reimbursement draft saved.",
            "success",
        )
        return redirect(url_for("employee_reimbursements.view_reimbursement", request_id=request_obj.id))
    except Exception as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("employee_reimbursements.new_reimbursement"))


@employee_reimbursements_bp.route("/<int:request_id>")
def view_reimbursement(request_id):
    employee = get_current_employee()
    if not employee:
        flash("A linked employee profile is required to view reimbursement requests.", "warning")
        return redirect(url_for("employee_reimbursements.list_reimbursements"))
    reimbursement = ReimbursementRequest.query.get_or_404(request_id)
    if reimbursement.employee_id != employee.id:
        flash("You can view only your own reimbursement requests.", "danger")
        return redirect(url_for("employee_reimbursements.list_reimbursements"))
    return render_template(
        "employee/reimbursement_detail.html",
        base_template=get_base_template_for_role(get_current_role()),
        reimbursement=reimbursement,
        can_submit=reimbursement.status == STATUS_DRAFT,
    )


@employee_reimbursements_bp.route("/<int:request_id>/submit", methods=["POST"])
def submit_reimbursement(request_id):
    employee = get_current_employee()
    if not employee:
        flash("A linked employee profile is required to submit reimbursement requests.", "danger")
        return redirect(url_for("employee_reimbursements.list_reimbursements"))
    reimbursement = ReimbursementRequest.query.get_or_404(request_id)
    if reimbursement.employee_id != employee.id:
        flash("You can submit only your own reimbursement request.", "danger")
        return redirect(url_for("employee_reimbursements.list_reimbursements"))
    if reimbursement.status != STATUS_DRAFT:
        flash("Only draft reimbursement requests can be submitted.", "warning")
        return redirect(url_for("employee_reimbursements.view_reimbursement", request_id=request_id))

    try:
        config = get_or_create_reimbursement_config()
        manager_approver = resolve_manager_approver(employee, config)
        if not manager_approver:
            raise ValueError("No reimbursement approver is configured for this employee.")

        reimbursement.status = STATUS_PENDING_MANAGER
        reimbursement.manager_approver_user_id = manager_approver.id
        reimbursement.current_assignee_user_id = manager_approver.id
        reimbursement.submitted_at = db.func.now()
        record_action(
            request_obj=reimbursement,
            action_by_user_id=employee.user_id,
            action_type="submitted",
            from_status=STATUS_DRAFT,
            to_status=STATUS_PENDING_MANAGER,
        )
        db.session.commit()
        send_reimbursement_submitted_email(reimbursement)
        flash("Reimbursement submitted for manager review.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(str(exc), "danger")

    return redirect(url_for("employee_reimbursements.view_reimbursement", request_id=request_id))


@employee_reimbursements_bp.route("/<int:request_id>/download-pdf")
def download_pdf(request_id):
    employee = get_current_employee()
    if not employee:
        flash("A linked employee profile is required to download reimbursement summaries.", "warning")
        return redirect(url_for("employee_reimbursements.list_reimbursements"))
    reimbursement = ReimbursementRequest.query.get_or_404(request_id)
    if reimbursement.employee_id != employee.id:
        flash("You can download only your own reimbursement summary.", "danger")
        return redirect(url_for("employee_reimbursements.list_reimbursements"))
    return render_reimbursement_pdf(reimbursement, "employee")
