"""Part GL121123 parameters (copied from desktop config.py)."""
DEFAULT_PARAMS = [
    {"key": "thread_len", "name": "Thread length", "nominal": 17.5,
     "tol_minus": 0.0, "tol_plus": 0.5, "unit": "mm", "enabled": True,
     "critical": False, "assumed": False, "offset": 0.0},
    {"key": "shoulder", "name": "Shoulder (Ø12→Ø14)", "nominal": 49.2,
     "tol_minus": 0.0, "tol_plus": 0.2, "unit": "mm", "enabled": True,
     "critical": True, "assumed": False, "offset": 0.0},
    {"key": "bend_start", "name": "Length to bend", "nominal": 59.0,
     "tol_minus": 0.0, "tol_plus": 0.3, "unit": "mm", "enabled": True,
     "critical": False, "assumed": False, "offset": 0.0},
    {"key": "d12", "name": "Dia Ø12", "nominal": 12.0,
     "tol_minus": 0.05, "tol_plus": 0.05, "unit": "mm", "enabled": True,
     "critical": True, "assumed": False, "offset": 0.0},
    {"key": "d14", "name": "Dia Ø14", "nominal": 14.0,
     "tol_minus": 0.2, "tol_plus": 0.2, "unit": "mm", "enabled": True,
     "critical": False, "assumed": True, "offset": 0.0},
    {"key": "angle", "name": "Bend angle", "nominal": 41.5,
     "tol_minus": 0.5, "tol_plus": 0.5, "unit": "deg", "enabled": True,
     "critical": False, "assumed": True, "offset": 0.0},
    {"key": "drop", "name": "Drop of bent end", "nominal": 37.7,
     "tol_minus": 0.3, "tol_plus": 0.3, "unit": "mm", "enabled": True,
     "critical": False, "assumed": True, "offset": 0.0},
    {"key": "thread_od", "name": "Thread OD (info)", "nominal": 9.87,
     "tol_minus": 0.11, "tol_plus": 0.10, "unit": "mm", "enabled": False,
     "critical": False, "assumed": True, "offset": 0.0},
    {"key": "axial_len", "name": "Axial length (ref)", "nominal": 101.6,
     "tol_minus": 1.0, "tol_plus": 1.0, "unit": "mm", "enabled": False,
     "critical": False, "assumed": True, "offset": 0.0},
]


def limits(p):
    return p["nominal"] - abs(p["tol_minus"]), p["nominal"] + abs(p["tol_plus"])
