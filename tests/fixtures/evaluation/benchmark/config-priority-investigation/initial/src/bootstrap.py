def resolve_settings(defaults, project_values, environment):
    resolved = {}
    resolved.update(environment)
    resolved.update(project_values)
    resolved.update(defaults)
    return resolved
