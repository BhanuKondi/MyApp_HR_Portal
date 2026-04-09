from decimal import Decimal

from flask import Blueprint, flash, redirect, render_template, request, url_for

from models.db import db
from models.models import ReimbursementRequest
from utils.authz import ROLE_MANAGER, get_current_employee, require_roles
from utils.reimbursement_service import (
    STATUS_PENDING_FINANCE,
    STATUS_PENDING_MANAGER,
    STATUS_REJECTED_MANAGER,
    ensure_transition,
    parse_amount,
    record_action,
)
from utils.workflow_email_service import (
    send_reimbursement_pending_finance_email,
    send_reimbursement_status_email,
)


manager_reimbursements_bp = Blueprint("manager_reimbursements", __name__, url_prefix="/manager/reimbursements")


@manager_reimbursements_bp.before_request
def enforce_manager_access():
    return require_roles(ROLE_MANAGER)


@manager_reimbursements_bp.route("")
def list_reimbursements():
    manager = get_current_employee()
    reimbursements = (
        ReimbursementRequest.query.filter_by(manager_approver_user_id=manager.user_id)
        .order_by(
            db.case((ReimbursementRequest.status == STATUS_PENDING_MANAGER, 0), else_=1),
            ReimbursementRequest.created_at.desc(),
        )
        .all()
    )
    summary = {
        "pending": sum(1 for item in reimbursements if item.status == STATUS_PENDING_MANAGER),
        "approved": sum(1 for item in reimbursements if item.status == STATUS_PENDING_FINANCE),
        "rejected": sum(1 for item in reimbursements if item.status == STATUS_REJECTED_MANAGER),
    }
    return render_template(
        "manager/reimbursements.html",
        reimbursements=reimbursements,
        summary=summary,
    )


@manager_reimbursements_bp.route("/<int:request_id>")
def view_reimbursement(request_id):
    manager = get_current_employee()
    reimbursement = ReimbursementRequest.query.get_or_404(request_id)
    if reimbursement.manager_approver_user_id != manager.user_id:
        flash("This reimbursement is not assigned to you.", "danger")
        return redirect(url_for("manager_reimbursements.list_reimbursements"))
    return render_template("manager/reimbursement_detail.html", reimbursement=reimbursement)


@manager_reimbursements_bp.route("/<int:request_id>/approve", methods=["POST"])
def approve_reimbursement(request_id):
    manager = get_current_employee()
    reimbursement = ReimbursementRequest.query.get_or_404(request_id)
    if reimbursement.manager_approver_user_id != manager.user_id:
        flash("This reimbursement is not assigned to you.", "danger")
        return redirect(url_for("manager_reimbursements.list_reimbursements"))

    try:
        ensure_transition(reimbursement, STATUS_PENDING_FINANCE)
        approved_amount_raw = request.form.get("approved_amount")
        approved_amount = parse_amount(approved_amount_raw) if approved_amount_raw else Decimal(reimbursement.requested_amount)
        comments = (request.form.get("comments") or "").strip() or None

        reimbursement.manager_approved_amount = approved_amount
        reimbursement.manager_comments = comments
        reimbursement.status = STATUS_PENDING_FINANCE
        reimbursement.current_assignee_user_id = None
        reimbursement.finance_approved_amount = None
        reimbursement.final_amount = None

        record_action(
            request_obj=reimbursement,
            action_by_user_id=manager.user_id,
            action_type="manager_approved",
            from_status=STATUS_PENDING_MANAGER,
            to_status=STATUS_PENDING_FINANCE,
            comments=comments,
        )
        db.session.commit()
        send_reimbursement_pending_finance_email(reimbursement)
        flash("Reimbursement forwarded to finance for review.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(str(exc), "danger")

    return redirect(url_for("manager_reimbursements.view_reimbursement", request_id=request_id))


@manager_reimbursements_bp.route("/<int:request_id>/reject", methods=["POST"])
def reject_reimbursement(request_id):
    manager = get_current_employee()
    reimbursement = ReimbursementRequest.query.get_or_404(request_id)
    if reimbursement.manager_approver_user_id != manager.user_id:
        flash("This reimbursement is not assigned to you.", "danger")
        return redirect(url_for("manager_reimbursements.list_reimbursements"))

    comments = (request.form.get("comments") or "").strip()
    if not comments:
        flash("Manager comments are required when rejecting a reimbursement.", "danger")
        return redirect(url_for("manager_reimbursements.view_reimbursement", request_id=request_id))

    try:
        ensure_transition(reimbursement, STATUS_REJECTED_MANAGER)
        reimbursement.manager_comments = comments
        reimbursement.status = STATUS_REJECTED_MANAGER
        reimbursement.current_assignee_user_id = None

        record_action(
            request_obj=reimbursement,
            action_by_user_id=manager.user_id,
            action_type="manager_rejected",
            from_status=STATUS_PENDING_MANAGER,
            to_status=STATUS_REJECTED_MANAGER,
            comments=comments,
        )
        db.session.commit()
        send_reimbursement_status_email(
            reimbursement,
            "Reimbursement Rejected By Manager",
            "Your reimbursement request was rejected during manager review.",
        )
        flash("Reimbursement rejected.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(str(exc), "danger")

    return redirect(url_for("manager_reimbursements.view_reimbursement", request_id=request_id))
