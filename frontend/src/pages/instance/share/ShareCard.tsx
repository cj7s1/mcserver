/** The Share card on the Overview and the header's Share button with the same content in a dialog (B1). */
import { Share2 } from "lucide-react";
import { useState } from "react";
import { Section } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import type { Instance } from "@/lib/types";
import { ShareContent } from "./ShareContent";

export function ShareCard({ instance }: { instance: Instance }) {
  return (
    <Section
      title={
        <span className="flex items-center gap-2">
          <Share2 className="size-4 text-muted-foreground" aria-hidden /> Share with friends
        </span>
      }
      description="Everything a friend needs to join, and a ready invite to paste into Discord"
    >
      <ShareContent instance={instance} />
    </Section>
  );
}

export function ShareButton({ instance }: { instance: Instance }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}>
        <Share2 /> Share
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>Share {instance.name}</DialogTitle>
            <DialogDescription>Everything a friend needs to join, and a ready invite to paste into Discord.</DialogDescription>
          </DialogHeader>
          {open && <ShareContent instance={instance} />}
        </DialogContent>
      </Dialog>
    </>
  );
}
