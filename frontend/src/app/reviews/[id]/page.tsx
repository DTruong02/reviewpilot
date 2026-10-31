"use client";
import {useParams} from "next/navigation";import {ReviewFinder} from "@/components/review-finder";
export default function ReviewPage(){const id=useParams<{id:string}>().id;return <ReviewFinder id={id}/>;}
