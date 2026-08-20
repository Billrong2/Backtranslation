protected SortedMap<Integer,RunT> _getRuns() {
        if(notLoaded || runs==null) {
            // if none is loaded yet, do so immediately.
            synchronized(this) {
                if(runs==null)
                    runs = new RunMap<RunT>();
                if(notLoaded) {
                    notLoaded = false;
                    _reload();   
                }
            }
        }
        if(nextUpdate<System.currentTimeMillis()) {
            if(!reloadingInProgress) {
                // schedule a new reloading operation.
                // we don't want to block the current thread,
                // so reloading is done asynchronously.
                reloadingInProgress = true;
                Set<ViewJob> reloadQueue;
                synchronized (ViewJob.class) {
                    if (reloadThread == null) {
                        reloadThread = new ReloadThread();
                        reloadThread.start();
                    }
                    reloadQueue = reloadThread.reloadQueue;
                }
                synchronized(reloadQueue) {
                    reloadQueue.add(this);
                    reloadQueue.notify();
                }
            }
        }
        return runs;
    }