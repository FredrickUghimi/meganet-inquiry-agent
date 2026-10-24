import logging

from flask import Flask

from .config import load_config
from .models import db


def create_app(overrides=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = Flask(__name__)
    app.config.update(load_config())
    if overrides:
        app.config.update(overrides)

    db.init_app(app)
    from . import dashboard, webhook
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(webhook.bp)

    with app.app_context():
        db.create_all()

    if app.config["SCHEDULER_ENABLED"]:
        from . import scheduler
        scheduler.start(app)
    return app
