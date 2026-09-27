def resolve_settings(defaults, project_values, environment):
    resolved = {}
    resolved.update(defaults)
    resolved.update(project_values)
    resolved.update(environment)
    return resolved
