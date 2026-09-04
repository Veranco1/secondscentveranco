import os
import secrets

from flask import Flask, render_template

from app.db import init_db
from app.auth import auth_bp
from app.orders import orders_bp
from app.disputes import disputes_bp
from app.payments import webhooks_bp
from app.admin import admin_bp
from app.listings import listings_bp
from app.web import web_bp
from app.i18n import t as _t, get_lang as _get_lang


def create_app(test_config=None):
    app = Flask(__name__)
    app.secret_key = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("FORCE_HTTPS", "0") == "1",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
        JSON_SORT_KEYS=False,
    )
    if test_config:
        app.config.update(test_config)

    init_db()
    app.register_blueprint(auth_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(disputes_bp)
    app.register_blueprint(webhooks_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(listings_bp)
    app.register_blueprint(web_bp)

    app.jinja_env.globals["t"] = _t
    app.jinja_env.globals["current_lang"] = _get_lang

    @app.template_filter("datetimeformat")
    def datetimeformat(value):
        import datetime
        if not value:
            return "—"
        return datetime.datetime.fromtimestamp(int(value)).strftime("%d-%m-%Y %H:%M")

    @app.route("/health")
    def health():
        return {"ok": True}

    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", code=404,
                                message="Deze pagina bestaat niet (meer)."), 404

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("error.html", code=403,
                                message="Je hebt geen toegang tot deze pagina."), 403

    return app
