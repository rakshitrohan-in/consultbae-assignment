# Task 5 — Stretch: What Breaks at 5,000 Workers in a Weekend

Task 5 explores the specific breaking points that would happen when 5,000 workers use the application over the weekend.

This one page contains no computer code, as the focus remains on the system's scaling and failure-mode analysis. The exact application built in Task 3 is being tested under the heavy pressure of real gig-worker-scale today. High volume reveals structural weaknesses that aren't visible during the development phase. 
---

## What Breaks First
**1. The database called **SQLite** is the first thing that will fail when the worker traffic increases significantly. Since SQLite allows only one writer at a single time, other writing tasks will block or fail pretty quickly. Every other write process is forced to wait while one operation is currently in progress inside the software system. Concurrent submissions from 5,000 workers will start hitting many write locks and timeout errors. A launch spike makes several people submit inside the same second which causes the database to stop working. Failure happens in the database before the logic of the application breaks because of these heavy writing limitations. Writing data becomes the single most immediate breaking point since the app logic stays functional while the storage fails.

**2. Processing **audio files synchronously** on the request thread creates a major problem for the overall server performance. Currently, the ffmpeg and pydub analysis runs while the HTTP request is still being handled by the server.The user's browser must wait until the full audio analysis finishes before receiving any response. A server thread is held hostage for a long time by every concurrent submission during the audio decoding process. When a handful of workers do this at the same time, the entire app is stalled for everyone else. All users are affected when a small group of workers consumes all the available threads on the server machine.

**3. **Storing files on the local disk** will lead to failure as the storage capacity is really limited in this setup. All the audio files are currently saved to a local folder named uploads on a single server machine disk as no cloud exists. When the volume of data increases, the disk becomes full and the system has no redundancy at all.Every submitted recording is gone permanently if that one server stops working or crashes. Playback for the listing page is slowed down significantly as there is no CDN or distributed storage currently. The server will fail to function correctly when the local folder grows too large for the physical hardware capacity.

**4. **Protection against duplicate submissions** or retry attempts doesn't exist when the network connection is pretty weak or unstable. If the connection of a worker drops while they're uploading, the person will probably try to submit the file again. Poor mobile network conditions are really common among many gig-workers in the field today. Nothing is currently stopping duplicate submissions from being created when the 5,000 workers try to submit their files many times. Data which is really noisy and hard to clean is created since of these many duplicate retries by the workers.

**5. **Authentication and rate limiting** aren't present in the current version of the digital submission form used for Task 3. The form stays fully open, which means any person with the link can submit many times without any restriction. Any name or phone number can be entered by bots or people who perform accidental spam refreshes on their phones. This opens the door for junk data is created when the system finally reaches public-launch scale. Such a lack of security remains harmless at small scales, but the large worker volume makes the system very dangerous. The application is vulnerable to many types of abuse since no rate limiting or login has been added.


---

## What I'd Change Before Launch

| Problem | Fix |
|---|---|
| The SQLite database contains a single-writer lock, which creates delays during the writing process. | Switching to a stronger server-based database like Postgres or MySQL will help since these systems manage many simultaneous writes through a native process. |
| The local disk storage setup creates a single point of failure since everything sits on one server's disk with no backup at all. | Moving all the uploaded audio files to a cloud storage service like S3 instead of a local folder solves this, since the files no longer depend on one machine staying alive, and the storage space isn't limited by a single server's hardware anymore. |
| The audio analysis process stops other requests since the sound analysis happens immediately. | The server should accept the digital sound files right away and then the deep study of sounds is placed into a working position queue like (Celery or RQ), as this allows the system to send an immediate response to the working person using the platform.  |
| Nothing currently stops a worker from accidentally submitting the same recording twice if their connection drops mid-upload and they try again. | Adding a simple check that looks at the name and phone number together within a short time window prevents a retry from creating a second, duplicate entry in the database. |
| There is no control over how many times a person accesses the system daily. | Adding specific limits based on IP addresses or mobile contact numbers is helpful since a simple verification step like a one-time code reduces unwanted spam messages from non-human actors. |
| Single point of failure (one server) | Run the app across multiple instances behind a load balancer, so one crashed process doesn't take down submissions entirely |

---

## Cost Consideration

At 5,000 submissions, even short audio clips (say, 30 seconds average) start to add up in storage and bandwidth — this is a genuinely different cost profile than a handful of test recordings, and would need actual estimation (storage cost per GB, egress for playback) before committing to a launch-day budget, rather than assuming "it's just audio files, how big can it be."

---

## Summary

The current version of the project remains appropriate for its initial goal as it functions well as a demonstration to prove that the logic and the extraction of audio work correctly. The issues mentioned previously are not mistakes in the current code but represent the natural space that exists between a simple demonstration and a heavy system that supports five thousand people at once. The solutions listed above are used by a professional in a working position to make sure the system stays alive when real users begin to interact with the software on a large scale. Each adjustment is aimed at making the project strong enough to handle the pressure of many people using the service at the same time.

