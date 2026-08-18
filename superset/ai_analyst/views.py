"""Flask view serving the AI Analyst SPA page at /aianalyst/."""
from flask_appbuilder.security.decorators import has_access
from flask_appbuilder import expose

from superset.superset_typing import FlaskResponse
from superset.views.base import BaseSupersetView


class AiAnalystView(BaseSupersetView):
    route_base = "/aianalyst"
    class_permission_name = "AiAnalyst"

    @expose("/")
    @has_access
    def root(self) -> FlaskResponse:
        return super().render_app_template()
