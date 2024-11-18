from typing import List, TypeVar, Union

T = TypeVar("T")


def slice_list(input_list: list[T], slice_str: str):
    """
    Gibt eine Subliste basierend auf der Slice-Syntax im String zurück.

    :param input_list: Die Liste, die gesliced werden soll.
    :param slice_str: Ein String, der die Slice-Syntax wie in Python ausdrückt (z.B. "[1:3]", "[:-1]", "[-1]").
    :return: Die geslicte Subliste.
    """
    try:
        # Entferne eckige Klammern
        slice_str = slice_str.strip().strip("[]")

        # Überprüfe, ob es ein vollständiger Slice-Ausdruck ist
        if ":" in slice_str:
            # Splitte die Start, Stop und Step Werte
            parts = slice_str.split(":")
            start = int(parts[0]) if parts[0] else None
            stop = int(parts[1]) if len(parts) > 1 and parts[1] else None
            step = int(parts[2]) if len(parts) > 2 and parts[2] else None
            return input_list[slice(start, stop, step)]
        else:
            # Einzelne Indizes werden direkt geparst
            index = int(slice_str)
            return [input_list[index]]

    except (ValueError, IndexError) as e:
        raise ValueError(f"Ungültiger Slice-String: {slice_str}. Fehler: {e}")
