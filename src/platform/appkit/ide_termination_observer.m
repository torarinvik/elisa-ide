// The AppKit menu's `terminate:` action can end the process directly from
// NSApplication's run loop. Notify the Elisa application first so it can stop
// workers and release its session-private temporary files.

#import <Cocoa/Cocoa.h>

extern void designer_ide_app_will_terminate(void);

static id designer_ide_termination_observer;

void designer_ide_register_termination_observer(void) {
    if (designer_ide_termination_observer != nil) return;
    NSNotificationCenter *center = [NSNotificationCenter defaultCenter];
    designer_ide_termination_observer = [center
        addObserverForName:NSApplicationWillTerminateNotification
        object:NSApp
        queue:nil
        usingBlock:^(NSNotification *notification) {
            (void)notification;
            designer_ide_app_will_terminate();
        }];
}
