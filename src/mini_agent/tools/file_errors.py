"""Typed precondition failures for exact file edits."""


class EditPreconditionError(ValueError):
    """The requested edit cannot be applied without changing its parameters."""


class EditNoMatchError(EditPreconditionError):
    """The edit's old_string does not occur in the file."""


class EditMultipleMatchesError(EditPreconditionError):
    """The edit's old_string occurs more than once without replace_all."""
