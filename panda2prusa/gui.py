"""Minimal Tkinter GUI: python -m panda2prusa.gui

An improvement over the original: it inspects the file first (producer, plates, painted
triangles), lets you pick which plate(s) to convert, and reports what was written.
"""

from __future__ import annotations

import os
import threading
import traceback
from tkinter import (
    Tk,
    Toplevel,
    StringVar,
    Label,
    Button,
    Entry,
    Frame,
    filedialog,
    messagebox,
    END,
    Text,
    DISABLED,
    NORMAL,
)

from .convert import (
    convert_file,
    describe,
    extruder_conflicts,
    filament_label,
    suggest_extruder_map,
    used_filaments,
)


def _center_on_screen(win, min_w: int = 0, min_h: int = 0) -> None:
    """Place a window in the middle of the monitor."""
    win.update_idletasks()
    w = max(win.winfo_reqwidth(), min_w)
    h = max(win.winfo_reqheight(), min_h)
    x = (win.winfo_screenwidth() - w) // 2
    y = (win.winfo_screenheight() - h) // 2
    win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")


class App:
    def __init__(self, master: Tk):
        self.master = master
        master.title("Panda2Prusa - Bambu to Prusa 3mf converter")
        master.minsize(560, 360)

        self.input_var = StringVar()
        self.output_var = StringVar()
        self.plate_var = StringVar(value="all")

        row = 0
        Label(master, text="Input Bambu / Orca .3mf:").grid(row=row, column=0, sticky="w", padx=8, pady=(10, 2))
        row += 1
        Entry(master, textvariable=self.input_var, width=60).grid(row=row, column=0, padx=8, sticky="we")
        Button(master, text="Browse…", command=self.pick_input).grid(row=row, column=1, padx=(4, 8))
        row += 1
        Label(master, text="Output Prusa .3mf:").grid(row=row, column=0, sticky="w", padx=8, pady=(8, 2))
        row += 1
        Entry(master, textvariable=self.output_var, width=60).grid(row=row, column=0, padx=8, sticky="we")
        Button(master, text="Browse…", command=self.pick_output).grid(row=row, column=1, padx=(4, 8))
        row += 1
        Label(master, text="Plates (e.g. 'all' or '1' or '1,2'):").grid(row=row, column=0, sticky="w", padx=8, pady=(8, 2))
        row += 1
        Entry(master, textvariable=self.plate_var, width=20).grid(row=row, column=0, padx=8, sticky="w")
        row += 1
        Button(master, text="Convert", command=self.convert, height=2).grid(
            row=row, column=0, columnspan=2, padx=8, pady=8, sticky="we"
        )
        row += 1
        self.log = Text(master, height=10, wrap="word", state=DISABLED)
        self.log.grid(row=row, column=0, columnspan=2, padx=8, pady=(0, 10), sticky="nsew")
        master.grid_columnconfigure(0, weight=1)
        master.grid_rowconfigure(row, weight=1)
        _center_on_screen(master, 560, 360)

    # -- helpers --------------------------------------------------------------
    def _log(self, msg: str) -> None:
        self.log.config(state=NORMAL)
        self.log.insert(END, msg + "\n")
        self.log.see(END)
        self.log.config(state=DISABLED)

    def pick_input(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("3mf files", "*.3mf")])
        if not path:
            return
        self.input_var.set(path)
        base, _ = os.path.splitext(path)
        if not self.output_var.get():
            self.output_var.set(base + "_prusa.3mf")
        try:
            project = describe(path)
            plates = project.plate_ids or ["(single)"]
            self._log(f"Loaded: {os.path.basename(path)}")
            self._log(f"  producer: {project.producer or 'unknown'}")
            self._log(f"  plates:   {', '.join(str(p) for p in plates)}")
            used = used_filaments(project)
            if used:
                self._log(f"  filaments: {', '.join(filament_label(project, f) for f in used)}")
        except Exception as exc:
            self._log(f"  could not inspect: {exc}")

    def pick_output(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".3mf", filetypes=[("3mf files", "*.3mf")])
        if path:
            self.output_var.set(path)

    def convert(self) -> None:
        inp, outp = self.input_var.get(), self.output_var.get()
        if not inp or not outp:
            messagebox.showwarning("Missing paths", "Choose both an input and an output file.")
            return
        plates = None
        pv = self.plate_var.get().strip().lower()
        if pv and pv != "all":
            try:
                plates = [int(p) for p in pv.replace(",", " ").split()]
            except ValueError:
                messagebox.showwarning("Bad plate", "Plates must be 'all' or numbers like 1 or 1,2.")
                return
        try:
            project = describe(inp)
        except Exception as exc:
            messagebox.showerror("Cannot read file", str(exc))
            return
        used = used_filaments(project)
        if len(used) > 1 or used not in ([], [1]):
            self._ask_mapping(project, used, inp, outp, plates)
        else:
            self._start(inp, outp, plates, None)

    def _ask_mapping(self, project, used, inp, outp, plates) -> None:
        """This file has N colors — ask how to map them to Prusa extruders."""
        dlg = Toplevel(self.master)
        dlg.title("Filament mapping")
        dlg.transient(self.master)
        dlg.grab_set()
        Label(
            dlg,
            text=(
                f"This file uses {len(used)} filament(s).\n"
                "Choose the Prusa extruder/tool for each (e.g. a two-tool XL has 1 and 2):"
            ),
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 6))

        suggestion = suggest_extruder_map(used)
        entries: dict[int, Entry] = {}
        for i, f in enumerate(used, start=1):
            row = Frame(dlg)
            row.grid(row=i, column=0, columnspan=2, sticky="w", padx=10, pady=2)
            color = None
            if 0 < f <= len(project.filament_colors):
                c = project.filament_colors[f - 1]
                color = c[:7] if c.startswith("#") and len(c) >= 7 else None
            sw = Label(row, text="    ", relief="solid", bd=1)
            if color:
                try:
                    sw.config(bg=color)
                except Exception:
                    pass
            sw.pack(side="left", padx=(0, 6))
            Label(row, text=f"Bambu filament {filament_label(project, f)}  →  extruder").pack(side="left")
            e = Entry(row, width=4)
            e.insert(0, str(suggestion[f]))
            e.pack(side="left", padx=(6, 0))
            entries[f] = e

        def start_with(mapping) -> None:
            dlg.destroy()
            self._start(inp, outp, plates, mapping)

        def ok() -> None:
            try:
                mapping = {f: int(e.get()) for f, e in entries.items()}
            except ValueError:
                messagebox.showwarning("Bad mapping", "Extruder numbers must be integers.", parent=dlg)
                return
            problems = extruder_conflicts(used, mapping)
            if problems:
                messagebox.showwarning("Mapping conflict", "\n".join(problems), parent=dlg)
                return
            start_with(mapping)

        btns = Frame(dlg)
        btns.grid(row=len(used) + 1, column=0, columnspan=2, pady=10, padx=10, sticky="we")
        Button(btns, text="Convert", command=ok, width=12).pack(side="left")
        Button(btns, text="Keep original numbers", command=lambda: start_with({})).pack(side="left", padx=6)
        Button(btns, text="Cancel", command=dlg.destroy).pack(side="right")
        _center_on_screen(dlg)

    def _start(self, inp, outp, plates, extruder_map) -> None:
        self._log(f"Converting → {os.path.basename(outp)} …")
        threading.Thread(
            target=self._run, args=(inp, outp, plates, extruder_map), daemon=True
        ).start()

    def _run(self, inp, outp, plates, extruder_map) -> None:
        try:
            result = convert_file(inp, outp, plates=plates, extruder_map=extruder_map)
            s = result.stats
            remap = ""
            if s.extruder_map:
                remap = "  Extruders remapped: " + ", ".join(
                    f"{k}→{v}" for k, v in sorted(s.extruder_map.items())
                )
            self.master.after(0, lambda: self._log(
                f"Done. {s.objects_written} objects, {s.volumes_written} volumes, "
                f"{s.build_items_written} build items, "
                f"{s.painted_triangles} painted triangles.{remap}"
            ))
        except Exception:
            err = traceback.format_exc()
            self.master.after(0, lambda: self._log("ERROR:\n" + err))


def main() -> None:
    root = Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
