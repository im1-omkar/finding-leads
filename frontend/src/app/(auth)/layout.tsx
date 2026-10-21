import type { ReactNode } from "react";
import Portal from "@/app/portal";
export default function AuthLayout({ children }: { children: ReactNode }) { return <>{children}<Portal /></>; }
