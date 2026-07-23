from __future__ import annotations

import os

from .messages import format_exception as _format_exception

XL_CALCULATION_AUTOMATIC = -4105
XL_CALCULATION_MANUAL = -4135


class FatalExcelSessionError(RuntimeError):
    pass


def normalized_excel_path(path) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))


def configure_excel_app(xl_app) -> None:
    xl_app.Visible = False
    xl_app.DisplayAlerts = False
    xl_app.ScreenUpdating = False
    xl_app.EnableEvents = False
    for attr, value in (
        ("AskToUpdateLinks", False),
        ("Interactive", False),
        ("UserControl", False),
        ("AutomationSecurity", 3),
        ("AlertBeforeOverwriting", False),
    ):
        try:
            setattr(xl_app, attr, value)
        except Exception:
            pass


def workbooks_count(xl_app) -> tuple[int | None, Exception | None]:
    try:
        return int(xl_app.Workbooks.Count), None
    except Exception as exc:
        return None, exc


def workbook_matches_filename(workbook, filename: str) -> bool:
    try:
        full_name = workbook.FullName
    except Exception:
        return False
    return normalized_excel_path(full_name) == normalized_excel_path(filename)


def close_rejected_workbook(workbook) -> None:
    try:
        workbook.Close(SaveChanges=False)
    except Exception as exc:
        raise FatalExcelSessionError(
            f"Excel opened unexpected workbook and failed to close it: {_format_exception(exc)}"
        ) from exc


def opened_workbook_after_none(
    xl_app,
    prior_count: int | None,
    prior_count_exc: Exception | None,
    filename: str,
):
    if prior_count is None:
        detail = f": {_format_exception(prior_count_exc)}" if prior_count_exc is not None else ""
        raise FatalExcelSessionError(
            f"Excel returned no workbook and prior workbook count was unavailable{detail}"
        )
    try:
        current_count = int(xl_app.Workbooks.Count)
    except Exception as exc:
        raise FatalExcelSessionError(
            f"Excel returned no workbook and current workbook count was unavailable: {_format_exception(exc)}"
        ) from exc

    matched_workbook = None
    if current_count > prior_count:
        for index in range(current_count, prior_count, -1):
            try:
                workbook = xl_app.Workbooks(index)
            except Exception as exc:
                raise FatalExcelSessionError(
                    f"Excel returned no workbook and a new workbook was inaccessible: {_format_exception(exc)}"
                ) from exc
            if workbook_matches_filename(workbook, filename):
                if matched_workbook is None:
                    matched_workbook = workbook
                else:
                    close_rejected_workbook(workbook)
                continue
            close_rejected_workbook(workbook)
        return matched_workbook

    return None


def open_workbook_for_recalc(xl_app, filename: str):
    open_attempts = [
        dict(
            Filename=filename,
            UpdateLinks=0,
            ReadOnly=False,
            IgnoreReadOnlyRecommended=True,
            AddToMru=False,
            Notify=False,
            CorruptLoad=1,
        ),
        dict(
            Filename=filename,
            UpdateLinks=0,
            ReadOnly=False,
            IgnoreReadOnlyRecommended=True,
            AddToMru=False,
        ),
        dict(Filename=filename, UpdateLinks=False, ReadOnly=False),
    ]
    last_exc: Exception | None = None
    for kwargs in open_attempts:
        prior_count, prior_count_exc = workbooks_count(xl_app)
        try:
            xl_book = xl_app.Workbooks.Open(**kwargs)
        except Exception as exc:
            last_exc = exc
            continue
        if xl_book is not None:
            return xl_book
        xl_book = opened_workbook_after_none(xl_app, prior_count, prior_count_exc, filename)
        if xl_book is not None:
            return xl_book
        last_exc = RuntimeError("Excel returned no workbook from Workbooks.Open")
    raise RuntimeError(f"Excel failed to open workbook: {last_exc}") from last_exc


def recalc_and_save_workbook(xl_app, filename: str) -> None:
    xl_book = None
    operation_error: Exception | None = None
    close_error: Exception | None = None
    previous_calculation = None
    save_calculation = XL_CALCULATION_AUTOMATIC
    try:
        try:
            previous_calculation = xl_app.Calculation
        except Exception:
            previous_calculation = None
        save_calculation = (
            previous_calculation
            if previous_calculation is not None and previous_calculation != XL_CALCULATION_MANUAL
            else XL_CALCULATION_AUTOMATIC
        )
        try:
            xl_app.Calculation = XL_CALCULATION_MANUAL
        except Exception:
            pass

        xl_book = open_workbook_for_recalc(xl_app, filename)

        try:
            xl_app.Calculation = save_calculation
        except Exception:
            try:
                xl_app.Calculation = XL_CALCULATION_AUTOMATIC
            except Exception:
                pass

        calculated_ok = False
        try:
            xl_app.CalculateFullRebuild()
            calculated_ok = True
        except Exception:
            try:
                xl_app.Calculate()
            except Exception:
                calculated_ok = False
            else:
                calculated_ok = True

        if not calculated_ok:
            raise RuntimeError("Excel calculation failed")

        try:
            xl_app.CalculateUntilAsyncQueriesDone()
        except Exception:
            pass

        try:
            xl_app.Calculation = save_calculation
        except Exception:
            pass

        xl_book.Save()
    except Exception as exc:
        operation_error = exc
        raise
    finally:
        if xl_book is not None:
            try:
                xl_book.Close(SaveChanges=False)
            except Exception as exc:
                close_error = exc
        try:
            xl_app.Calculation = save_calculation
        except Exception:
            pass
        if close_error is not None:
            if operation_error is not None:
                raise FatalExcelSessionError(
                    f"Excel failed to close workbook: {_format_exception(close_error)}; "
                    f"prior failure: {_format_exception(operation_error)}"
                ) from close_error
            raise FatalExcelSessionError(
                f"Excel failed to close workbook: {_format_exception(close_error)}"
            ) from close_error
