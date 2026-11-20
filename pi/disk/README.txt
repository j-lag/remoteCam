RemoteCam
=========

This drive is provided by the RemoteCam Raspberry Pi.

1. Open remotecam.ini with Notepad.
2. Enter the WiFi of the network where the PC sharing its screen is
   (section [wifi1]: ssid and password), then save.
3. After ~10 s the Raspberry Pi joins that WiFi. Its IP address is shown
   in the picture of the "RemoteCam" camera (e.g. Teams camera preview).
4. On the source PC, start the RemoteCam application: it finds the
   Raspberry Pi automatically and starts sending the selected screen.
5. In Teams: Settings > Devices > Camera > RemoteCam.

Full resolution screen sharing in Teams:
   open viewer.html (double-click, opens in Edge / Chrome), allow the
   camera, press F for full screen, then share that screen in Teams.
   Teams screen sharing keeps the full resolution, a Teams camera does not.

STATUS.TXT shows the state of the last session (WiFi, IP address).
