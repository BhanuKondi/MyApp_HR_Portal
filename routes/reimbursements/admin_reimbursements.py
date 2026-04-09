from flask import Blueprint, flash, redirect, render_template, request, url_for

from models.db import db
from models.models import ReimbursementRequest, ReimbursementType, User
from utils.authz import ROLE_ACCOUNT_ADMIN, ROLE_ADMIN, get_role_by_name, require_roles
from utils.reimbursement_service import get_or_create_reimbursement_config, seed_reimbursement_types


admin_reimbursements_bp = Blueprint("admin_reimbursements", __name__, url_prefix="/admin/reimbursements")


@admin_reimbursements_bp.before_request
def enforce_admin_access():
    return require_roles(ROLE_ADMIN)


@admin_reimbursements_bp.route("/settings", methods=["GET", "POST"])
def settings():
    seed_reimbursement_types()
    config = get_or_create_reimbursement_config()
    account_admin_role = get_role_by_name(ROLE_ACCOUNT_ADMIN)
    finance_users = (
        User.query.filter_by(role_id=account_admin_role.id).order_by(User.display_name.asc(), User.email.asc()).all()
        if account_admin_role
        else []
    )
    approver_users = User.query.order_by(User.display_name.asc(), User.email.asc()).all()

    if request.method == "POST":
        config.approver_mode = request.form.get("approver_mode", "reporting_manager")
        fixed_user_id = request.form.get("fixed_approver_user_id")
        config.fixed_approver_user_id = int(fixed_user_id) if fixed_user_id else None
        config.allow_partial_approval = request.form.get("allow_partial_approval") == "true"
        config.allow_multiple_attachments = request.form.get("allow_multiple_attachments") == "true"
        db.session.commit()
        flash("Reimbursement settings updated successfully.", "success")
        return redirect(url_for("admin_reimbursements.settings"))

    return render_template(
        "admin/reimbursement_settings.html",
        config=config,
        finance_users=finance_users,
        approver_users=approver_users,
    )


@admin_reimbursements_bp.route("/types", methods=["GET", "POST"])
def types():
    seed_reimbursement_types()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        description = (request.form.get("description") or "").strip()
        if not name:
            flash("Type name is required.", "danger")
            return redirect(url_for("admin_reimbursements.types"))

        if ReimbursementType.query.filter(db.func.lower(ReimbursementType.name) == name.lower()).first():
            flash("That reimbursement type already exists.", "warning")
            return redirect(url_for("admin_reimbursements.types"))

        db.session.add(ReimbursementType(name=name, description=description or None, is_active=True))
        db.session.commit()
        flash("Reimbursement type added.", "success")
        return redirect(url_for("admin_reimbursements.types"))

    types_list = ReimbursementType.query.order_by(ReimbursementType.is_active.desc(), ReimbursementType.name.asc()).all()
    return render_template("admin/reimbursement_types.html", reimbursement_types=types_list)


@admin_reimbursements_bp.route("/types/<int:type_id>/toggle", methods=["POST"])
def toggle_type(type_id):
    reimbursement_type = ReimbursementType.query.get_or_404(type_id)
    reimbursement_type.is_active = not reimbursement_type.is_active
    db.session.commit()
    flash("Reimbursement type updated.", "success")
    return redirect(url_for("admin_reimbursements.types"))


@admin_reimbursements_bp.route("/reports")
def reports():
    reimbursements = ReimbursementRequest.query.order_by(ReimbursementRequest.created_at.desc()).all()
    summary = {
        "submitted": sum(1 for item in reimbursements if item.status in {"pending_manager", "pending_finance"}),
        "approved": sum(1 for item in reimbursements if item.status == "approved_for_payment"),
        "paid": sum(1 for item in reimbursements if item.status == "paid"),
        "rejected": sum(1 for item in reimbursements if item.status in {"rejected_by_manager", "rejected_by_finance"}),
    }
    return render_template(
        "admin/reimbursement_reports.html",
        reimbursements=reimbursements,
        summary=summary,
    )
