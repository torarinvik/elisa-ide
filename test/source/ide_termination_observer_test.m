#import <Cocoa/Cocoa.h>

extern void designer_ide_register_termination_observer(void);

static int termination_callback_count;

void designer_ide_app_will_terminate(void) {
    termination_callback_count++;
}

int main(void) {
    [NSApplication sharedApplication];
    designer_ide_register_termination_observer();
    designer_ide_register_termination_observer();
    [[NSNotificationCenter defaultCenter]
        postNotificationName:NSApplicationWillTerminateNotification
        object:NSApp];
    if (termination_callback_count != 1) {
        fprintf(stderr, "expected one termination callback, received %d\n",
                termination_callback_count);
        return 1;
    }
    puts("test ide_termination_observer: ok");
    return 0;
}
