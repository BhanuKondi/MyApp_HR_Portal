from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for

from models.db import db
from models.models import AccountsRequest
from utils.accounts_request_service import (
    STATUS_APPROVED,
    STATUS_PENDING_APPROVAL,
    STATUS_REJECTED,
    ensure_transition,
    parse_amount,
    record_action,
)
from utils.accounts_request_pdf import render_accounts_request_pdf
from utils.authz import ROLE_MANAGER, get_current_user, require_roles
from utils.workflow_email_service import send_accounts_request_status_email


manager_account_requests_bp = Blueprint(
    "manager_account_requests",
    __name__,
    url_prefix="/manager/accounts",
)


@manager_account_requests_bp.before_request
def enforce_manager_access():
    return require_roles(ROLE_MANAGER)


@manager_account_requests_bp.route("/requests")
def list_requests():
    current_user = get_current_user()
    requests_list = (
        AccountsRequest.query.filter_by(approver_user_id=current_user.id)
        .order_by(
            db.case((AccountsRequest.status == STATUS_PENDING_APPROVAL, 0), else_=1),
            AccountsRequest.created_at.desc(),
        )
        .all()
    )
    summary = {
        "pending_approval": sum(1 for item in requests_list if item.status == STATUS_PENDING_APPROVAL),
        "approved": sum(1 for item in requests_list if item.status == STATUS_APPROVED),
        "rejected": sum(1 for item in requests_list if item.status == STATUS_REJECTED),
    }
    return render_template(
        "manager/accounts_requests.html",
        requests_list=requests_list,
        summary=summary,
    )


@manager_account_requests_bp.route("/requests/<int:request_id>")
def view_request(request_id):
    current_user = get_current_user()
    accounts_request = AccountsRequest.query.get_or_404(request_id)
    if accounts_request.approver_user_id != current_user.id:
        flash("This request is not assigned to you.", "danger")
        return redirect(url_for("manager_account_requests.list_requests"))
    return render_template("manager/accounts_request_detail.html", accounts_request=accounts_request)


@manager_account_requests_bp.route("/requests/<int:request_id>/download-summary")
def download_summary(request_id):
    current_user = get_current_user()
    accounts_request = AccountsRequest.query.get_or_404(request_id)
    if accounts_request.approver_user_id != current_user.id:
        flash("This request is not assigned to you.", "danger")
        return redirect(url_for("manager_account_requests.list_requests"))
    return render_accounts_request_pdf(accounts_request, "manager")


@manager_account_requests_bp.route("/requests/<int:request_id>/approve", methods=["POST"])
def approve_request(request_id):
    current_user = get_current_user()
    accounts_request = AccountsRequest.query.get_or_404(request_id)
    if accounts_request.approver_user_id != current_user.id:
        flash("This request is not assigned to you.", "danger")
        return redirect(url_for("manager_account_requests.list_requests"))

    try:
        ensure_transition(accounts_request, STATUS_APPROVED)
        approved_amount = parse_amount(request.form.get("approved_amount"))
        approval_comments = (request.form.get("approval_comments") or "").strip() or None
        accounts_request.approved_amount = approved_amount
        accounts_request.approval_comments = approval_comments
        accounts_request.status = STATUS_APPROVED
        accounts_request.approved_at = datetime.utcnow()
        record_action(
            request_obj=accounts_request,
            action_by_user_id=current_user.id,
            from_status=STATUS_PENDING_APPROVAL,
            to_status=STATUS_APPROVED,
            action_type="approved",
            comments=approval_comments,
        )
        db.session.commit()
        send_accounts_request_status_email(
            accounts_request,
            "Accounts Request Approved",
            "Your accounts request has been approved and is ready for expense execution.",
        )
        flash("Accounts request approved.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("manager_account_requests.view_request", request_id=request_id))


@manager_account_requests_bp.route("/requests/<int:request_id>/reject", methods=["POST"])
def reject_request(request_id):
    current_user = get_current_user()
    accounts_request = AccountsRequest.query.get_or_404(request_id)
    if accounts_request.approver_user_id != current_user.id:
        flash("This request is not assigned to you.", "danger")
        return redirect(url_for("manager_account_requests.list_requests"))

    rejection_comments = (request.form.get("approval_comments") or "").strip()
    if not rejection_comments:
        flash("Comments are required when rejecting a request.", "danger")
        return redirect(url_for("manager_account_requests.view_request", request_id=request_id))

    try:
        ensure_transition(accounts_request, STATUS_REJECTED)
        accounts_request.approval_comments = rejection_comments
        accounts_request.status = STATUS_REJECTED
        record_action(
            request_obj=accounts_request,
            action_by_user_id=current_user.id,
            from_status=STATUS_PENDING_APPROVAL,
            to_status=STATUS_REJECTED,
            action_type="rejected",
            comments=rejection_comments,
        )
        db.session.commit()
        send_accounts_request_status_email(
            accounts_request,
            "Accounts Request Rejected",
            "Your accounts request was rejected during approval review.",
        )
        flash("Accounts request rejected.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("manager_account_requests.view_request", request_id=request_id))
