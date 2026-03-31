# Room-Drop: The Room Based Private File Sharing Application
## An Overview
-  **Room Drop** is a temporary, browser-based system that allows users to share files and text in isolated rooms identified by unique Room IDs.
-  Instead of relying on physical network detection, the system logically groups users using server-generated Room IDs.
-  Each room functions as an independent environment managed by a backend server and database.
-  The system supports real-time updates using persistent connections and automatically removes expired rooms and associated data.
## Main Goals
- To design a relational database structure for managing rooms, devices, files, and messages.
- To implement real-time file and text sharing using WebSockets.
- To ensure temporary and isolated room-based access control.
- To implement automatic expiry and cleanup mechanisms.
- To build a lightweight and accessible web-based interface.

