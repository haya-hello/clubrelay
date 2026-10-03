from .services import is_reviewer

def navigation(request):
    return {"reviewer": is_reviewer(request.user)}

