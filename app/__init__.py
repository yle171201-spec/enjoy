"""Application package.

Includes a Starlette/Jinja2 compatibility shim because recent Starlette releases
expect ``TemplateResponse(request=..., name=..., context=...)`` while the V2
page handlers used the older positional form.
"""

from starlette.templating import Jinja2Templates

_original_template_response = Jinja2Templates.TemplateResponse


def _template_response_compat(self, *args, **kwargs):
    if len(args) >= 2 and isinstance(args[0], str) and isinstance(args[1], dict):
        name = args[0]
        context = args[1]
        request = context.get("request")
        if request is None:
            raise ValueError("Template context must contain 'request'")
        return _original_template_response(
            self,
            request=request,
            name=name,
            context=context,
            **kwargs,
        )
    return _original_template_response(self, *args, **kwargs)


Jinja2Templates.TemplateResponse = _template_response_compat
