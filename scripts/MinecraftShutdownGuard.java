// SPDX-License-Identifier: MIT
// Narrow recovery for PFM 1.4.4's orphan writer AFTER Minecraft and main have exited.
// Never kills Java, calls System.exit, stops the host, or interrupts a server thread.
import java.lang.instrument.Instrumentation;
import java.lang.reflect.Field;
import java.util.*;
import java.util.concurrent.ThreadPoolExecutor;
import com.sun.tools.attach.VirtualMachine;

public class MinecraftShutdownGuard {
    private static Object field(Object object, String name) throws Exception {
        Field f=object.getClass().getDeclaredField(name); f.setAccessible(true); return f.get(object);
    }
    private static boolean writer(Thread t, StackTraceElement[] stack) {
        return t.isAlive() && !t.isDaemon() && t.getState()==Thread.State.WAITING
            && Arrays.stream(stack).anyMatch(s -> s.getClassName().equals("com.unlikepaladin.pfm.runtime.PFMProvider") && s.getMethodName().equals("lambda$createWriter$1"))
            && Arrays.stream(stack).anyMatch(s -> s.getClassName().equals("java.util.concurrent.LinkedBlockingQueue") && s.getMethodName().equals("take"));
    }
    public static void agentmain(String mode, Instrumentation inst) throws Exception {
        var threads=Thread.getAllStackTraces();
        var targets=new ArrayList<Thread>(); boolean mainReturned=false;
        for (var e:threads.entrySet()) {
            Thread t=e.getKey();
            if (t.getName().equals("Server thread") && t.isAlive()) throw new IllegalStateException("Minecraft server thread still exists");
            if (!t.isAlive() || t.isDaemon() || t==Thread.currentThread()) continue;
            if (t.getName().equals("DestroyJavaVM")) { mainReturned=true; continue; }
            if (writer(t,e.getValue())) targets.add(t);
            else throw new IllegalStateException("Unrecognized non-daemon thread: "+t.getName());
        }
        if (!mainReturned || targets.isEmpty()) throw new IllegalStateException("No verified post-server PFM orphan");
        var self=MinecraftShutdownGuard.class.getModule();
        inst.redefineModule(Thread.class.getModule(),Set.of(),Map.of(),Map.of("java.lang",Set.of(self),"java.util.concurrent",Set.of(self)),Set.of(),Map.of());
        for (Thread t:targets) {
            Object holder=field(t,"holder"), task=field(holder,"task");
            if (!task.getClass().getName().equals("java.util.concurrent.ThreadPoolExecutor$Worker")) throw new IllegalStateException("Unexpected worker type");
            var executor=(ThreadPoolExecutor)field(task,"this$0");
            if (executor.getPoolSize()!=1 || executor.getActiveCount()!=1 || !executor.getQueue().isEmpty()) throw new IllegalStateException("Executor is not an isolated idle writer");
            Thread.sleep(1000);
            if (!writer(t,t.getStackTrace()) || !executor.getQueue().isEmpty()) throw new IllegalStateException("Writer state changed; refusing recovery");
            System.out.println("[MinecraftShutdownGuard] Verified empty PFM writer after server exit: "+t.getName()+"; mode="+mode);
            if (mode.equals("release")) {
                executor.shutdown(); // no task is killed or discarded
                t.interrupt(); // PFM catches InterruptedException, completes its latch and returns
                t.join(5000);
                System.out.println("[MinecraftShutdownGuard] PFM writer exited="+!t.isAlive());
            }
        }
    }
    public static void main(String[] args) throws Exception {
        if(args.length!=2 || !(args[1].equals("audit") || args[1].equals("release"))) throw new IllegalArgumentException("PID audit|release");
        String jar=new java.io.File(MinecraftShutdownGuard.class.getProtectionDomain().getCodeSource().getLocation().toURI()).getAbsolutePath();
        VirtualMachine vm=VirtualMachine.attach(args[0]);
        try { vm.loadAgent(jar,args[1]); } finally { vm.detach(); }
    }
}
