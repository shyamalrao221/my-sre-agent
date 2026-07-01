# project_context.py

PROJECT_CONTEXT = {}

def set_project_context(data: dict):
    global PROJECT_CONTEXT
    PROJECT_CONTEXT = data

def get_project_context():
    return PROJECT_CONTEXT
