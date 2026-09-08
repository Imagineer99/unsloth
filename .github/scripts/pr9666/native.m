// SPDX-License-Identifier: AGPL-3.0-only
// Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
#import <AppKit/AppKit.h>
#import <WebKit/WebKit.h>
#include <stdlib.h>
#include <string.h>

// Called only on Tauri's UI thread. Native frames are expressed in the
// webview's top-left coordinate system, in points, independently of CSS zoom.
char *pr9666_geometry(void *pointer) {
    @autoreleasepool {
        WKWebView *view = (__bridge WKWebView *)pointer;
        NSWindow *window = view.window;
        if (!window) return NULL;
        NSMutableArray *buttons = [NSMutableArray array];
        NSArray *names = @[@"close", @"minimize", @"zoom"];
        NSWindowButton kinds[] = {NSWindowCloseButton, NSWindowMiniaturizeButton, NSWindowZoomButton};
        for (NSUInteger i = 0; i < 3; ++i) {
            NSButton *button = [window standardWindowButton:kinds[i]];
            if (!button) continue;
            NSRect r = [button convertRect:button.bounds toView:view];
            double top = view.isFlipped ? r.origin.y : NSHeight(view.bounds) - NSMaxY(r);
            [buttons addObject:@{@"name": names[i], @"x": @(r.origin.x), @"y": @(top),
                @"width": @(r.size.width), @"height": @(r.size.height),
                @"hidden": @(button.isHidden), @"enabled": @(button.isEnabled)}];
        }
        NSDictionary *facts = @{
            @"page_zoom": @(view.pageZoom), @"window_number": @(window.windowNumber),
            @"window_visible": @(window.isVisible), @"key_window": @(window.isKeyWindow),
            @"view_width": @(NSWidth(view.bounds)), @"view_height": @(NSHeight(view.bounds)),
            @"backing_scale": @(window.backingScaleFactor), @"buttons": buttons,
            @"platform": [[NSProcessInfo processInfo] operatingSystemVersionString]
        };
        NSData *data = [NSJSONSerialization dataWithJSONObject:facts options:0 error:NULL];
        if (!data) return NULL;
        char *result = malloc(data.length + 1);
        if (!result) return NULL;
        memcpy(result, data.bytes, data.length);
        result[data.length] = 0;
        return result;
    }
}

void pr9666_free(char *pointer) { free(pointer); }
