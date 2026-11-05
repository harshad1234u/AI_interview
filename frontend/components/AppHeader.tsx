"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import AccountBar from "@/components/AccountBar";
import { useAuth } from "@/components/AuthProvider";
import ThemeToggle from "@/components/ThemeToggle";

/** The top bar on every page: brand, navigation, theme toggle, and auth controls. */
export default function AppHeader() {
  const { candidate } = useAuth();
  const path = usePathname();

  const link = (href: string, label: string) => (
    <Link href={href} className="nav-link" aria-current={path === href ? "page" : undefined}>
      {label}
    </Link>
  );

  return (
    <header className="app-header">
      <div className="app-header-inner">
        <Link href="/" className="brand" aria-label="AI Interview Coach, home">
          <span className="brand-mark" aria-hidden="true">AI</span>
          <span className="brand-name">Interview Coach</span>
        </Link>

        {candidate ? (
          <nav className="nav" aria-label="Main">
            {link("/", "Dashboard")}
            {link("/interview", "Interviews")}
          </nav>
        ) : (
          <nav className="nav" aria-label="Product navigation">
            <Link href="/#features" className="nav-link">Features</Link>
            <Link href="/#how-it-works" className="nav-link">How it Works</Link>
            <Link href="/#benefits" className="nav-link">Benefits</Link>
          </nav>
        )}

        <div className="header-end">
          <ThemeToggle />
          {candidate ? (
            <AccountBar />
          ) : path !== "/login" ? (
            <Link href="/login" className="button-link primary" style={{ padding: "0.45rem 1rem", fontSize: "0.85rem" }}>
              Sign In
            </Link>
          ) : null}
        </div>
      </div>
    </header>
  );
}
