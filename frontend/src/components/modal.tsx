"use client";

import { useEffect, useRef, type ReactNode } from "react";

/** Accessible modal dialog built on the native <dialog> element (focus trap and Esc handling included). */
export function Modal({ open, title, onClose, children, footer }: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      onClose={onClose}
      onCancel={(e) => { e.preventDefault(); onClose(); }}
      className="m-auto w-[min(36rem,calc(100vw-2rem))] rounded-lg border border-border bg-surface p-0 text-foreground backdrop:bg-black/40"
      aria-labelledby="modal-title"
    >
      {open ? (
        <div className="p-5">
          <h2 id="modal-title" className="text-base font-semibold">{title}</h2>
          <div className="mt-4">{children}</div>
          {footer ? <div className="mt-5 flex flex-wrap justify-end gap-2">{footer}</div> : null}
        </div>
      ) : null}
    </dialog>
  );
}
